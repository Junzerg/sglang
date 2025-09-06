import torch
import triton
import triton.language as tl

from sglang.srt.lora.utils import LoRABatchInfo


@triton.jit
def _sgemm_lora_a_kernel_chunked(
    # Pointers to matrices
    x,
    weights,
    output,
    # Matrix dimensions
    N,  # stack_num * r
    K,  # input_dim
    stack_num,
    # Strides
    x_stride_0,
    x_stride_1,
    w_stride_0,
    w_stride_1,
    w_stride_2,
    output_stride_0,
    output_stride_1,
    # Information on sequence lengths,ranks and weight id
    cu_chunk_lens,
    index_map,
    chunk_to_weight,
    lora_ranks,
    num_chunks,
    # Meta parameters
    BLOCK_S: tl.constexpr,
    BLOCK_N: tl.constexpr,
    BLOCK_K: tl.constexpr,
):
    """
    Computes a segmented batched matrix multiplication for the LoRA A matrix.

    The kernel ensures that output[seg_start:seg_start + seg_len, :rank * stack_num]
    stores the product of the input `x` and the LoRA weights for the corresponding
    sequence. This implies that when rank is 0, the kernel is essentially a no-op,
    as output[seg_start:seg_start + seg_len, :0] is trivially correct (empty).

    Args:
        x (torch.Tensor): The input activations tensor of shape `(s, K)`, where `s`
            is the sum of all sequence lengths in the batch.
        weights (torch.Tensor): The LoRA 'A' weights for all available adapters,
            with shape `(num_lora, N, K)`.
        output (torch.Tensor): The output tensor of shape `(s, N)`.
    """
    chunk_id = tl.program_id(1)
    if chunk_id >= num_chunks:
        return 

    slice_id = tl.program_id(0)

    # Current block computes sequence with batch_id,
    # which starts from row seg_start of x with length seg_len
    w_index = tl.load(chunk_to_weight + chunk_id)
    rank = tl.load(lora_ranks + w_index)

    # If rank is 0, this kernel becomes a no-op as the output is always trivially correct.
    if rank == 0:
        return

    seg_start = tl.load(cu_chunk_lens + chunk_id)
    seg_end = tl.load(cu_chunk_lens + chunk_id + 1)

    # Adjust N (stack_num * max_rank) according to the specific LoRA adapter
    N = tl.minimum(N, rank * stack_num)

    # The tile in output matrix will have (pid_s, pid_n) as id

    # Create pointers for the first block of x and weights[batch_id]
    # The pointers will be advanced as we move in the K direction
    # and accumulate
    s_offset_orig = tl.arange(0, BLOCK_S) + seg_start
    s_offset = tl.load(index_map + s_offset_orig, mask=s_offset_orig < seg_end, other=0)  # (BLOCK_S,)

    n_offset = tl.arange(0, BLOCK_N) + slice_id * BLOCK_N
    k_offset = tl.arange(0, BLOCK_K)
    x_ptrs = x + (
        s_offset[:, None] * x_stride_0 + k_offset[None, :] * x_stride_1
    )
    w_ptrs = (weights + w_index * w_stride_0) + (
        k_offset[:, None] * w_stride_2 + n_offset[None, :] * w_stride_1
    )

    # Iterate to compute the block in output matrix
    partial_sum = tl.zeros((BLOCK_S, BLOCK_N), dtype=tl.float32)
    for k in range(0, tl.cdiv(K, BLOCK_K)):
        x_tile = tl.load(
            x_ptrs,
            mask=(s_offset[:, None] < seg_end) & (k_offset[None, :] < K - k * BLOCK_K),
            other=0.0,
        )
        w_tile = tl.load(
            w_ptrs,
            mask=(k_offset[:, None] < K - k * BLOCK_K) & (n_offset[None, :] < N),
            other=0.0,
        )
        partial_sum += tl.dot(x_tile, w_tile)

        x_ptrs += BLOCK_K * x_stride_1
        w_ptrs += BLOCK_K * w_stride_2

    # Store result to output matrix
    partial_sum = partial_sum.to(x.dtype.element_ty)
    output_ptr = output  + (
        s_offset[:, None] * output_stride_0 + n_offset[None, :] * output_stride_1
    )
    output_mask = (s_offset[:, None] < seg_end) & (n_offset[None, :] < N)
    tl.store(output_ptr, partial_sum, mask=output_mask)

def sgemm_lora_a_fwd_chunked(
    x: torch.Tensor,
    weights: torch.Tensor,
    batch_info: LoRABatchInfo,
    stack_num: int = 1,
) -> torch.Tensor:
    # x: (s, input_dim)
    # weights: (num_lora, stack_num * r, input_dim)
    # output: (s, stack_num * r)
    # stack_num: run_qkv_lora: 3, run_gate_up_lora: 2
    # when called by run_qkv_lora, the weights.shape[-2] will be 3 * r
    # input_dim is much larger than r

    assert x.is_contiguous()
    assert weights.is_contiguous()
    assert len(x.shape) == 2
    assert len(weights.shape) == 3

    # Block shapes
    BLOCK_S = 16
    BLOCK_N = 16
    BLOCK_K = 256

    S = x.shape[0]
    N = weights.shape[1]
    K = weights.shape[2]
    assert x.shape[-1] == K

    max_seq_len = batch_info.bs if batch_info.is_decode else S
    grid = (
        triton.cdiv(N, BLOCK_N),
        triton.cdiv(max_seq_len, BLOCK_S),
    )

    output = torch.empty((S, N), device=x.device, dtype=x.dtype)
    _sgemm_lora_a_kernel_chunked[grid](
        x,
        weights,
        output,
        N,
        K,
        stack_num,
        x.stride(0),
        x.stride(1),
        weights.stride(0),
        weights.stride(1),
        weights.stride(2),
        output.stride(0),
        output.stride(1),
        batch_info.cu_chunk_lens,
        batch_info.index_map,
        batch_info.chunk_to_weight,
        batch_info.lora_ranks,
        batch_info.num_chunks,
        BLOCK_S,
        BLOCK_N,
        BLOCK_K,
    )

    return output
