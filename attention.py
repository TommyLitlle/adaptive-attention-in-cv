import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
import numpy
import math

'''
This class is taken from https://github.com/facebookresearch/adaptive-span/blob/master/adaptive_span.py
but has been adapted to work with 2d inputs
'''
# TODO: Get this working with multiple heads
class AdaptiveMask(nn.Module):
    """Soft masking function for adaptive size.
    It masks out the last K values of an input. The masking value
    goes from 1 to 0 gradually, so K can be learned with
    back-propagation.
    Args:
        max_size: maximum size (i.e. input dimension)
        ramp_size: size of the ramp going from 0 to 1
        init_val: initial size proportion not to be masked out
        shape: learn multiple sizes independent of each other

    This will give give our symmetric mask for each attention kernel
    """

    def __init__(self, max_size, ramp_size, init_val=0, shape=(1,)):
        nn.Module.__init__(self)
        self._max_size = max_size
        self._ramp_size = ramp_size
        self.current_val = nn.Parameter(torch.zeros(*shape) + init_val)
        self.mask_template = torch.linspace(1 - max_size, 0, steps=max_size)

    def forward(self, x):

        one_d_mask = self.mask_template + self.current_val * self._max_size
        one_d_mask = one_d_mask / self._ramp_size + 1
        one_d_mask = one_d_mask.clamp(0, 1)
        # TODO Debug: Check that indexing right dim, this should be relative to x size
        #              if kernel is 3x3 then would expect x.shape[-1] to be 3
        one_d_mask = one_d_mask[-(x.shape[-1]//2):]

        # Now masking 'out' (how do we count distance? One way is to start at the center pixel of the kernel and
        # work outwards one square around it at a time filling in the mask.
        # For ex: the adjacent pixels to center pixel have same masking weight. Now pixels outside of those that are
        # adjacent have same weight and so on.
        mask = torch.ones(x.shape[-1], x.shape[-1])
        left, right = 0, x.shape[-1] - 1

        for i in range(len(one_d_mask)):
            bottom, top = left, right
            indices = [[left, j] for j in range(bottom, top + 1)]  # left edge indices
            indices += [[bottom, j] for j in range(left + 1, right + 1)]  # bottom edge minus overlap with left
            indices += [[top, j] for j in range(left + 1, right + 1)]  # top minus overlap with left
            indices += [[right, j] for j in range(bottom + 1, top)]  # right minus overlap with bottom and top
            rows, cols = zip(*indices)
            mask[rows, cols] = one_d_mask[i] # TODO: Check how should be indexing this

            left += 1
            right -= 1

        #if x.size(-1) < self._max_size:
        #    # the input could have been trimmed beforehand to save computation
        #    mask = mask[:, :, -x.size(-1):]

        x = x * mask  #TODO: check dimensions of mask to make sure this is broadcast along
                      #     proper dimension
        return x

    def get_current_max_size(self, include_ramp=True):
        current_size = math.ceil(self.current_val.detach().max().item() * self._max_size)
        if include_ramp:
            current_size += self._ramp_size
        current_size = max(0, min(self._max_size, current_size))
        return current_size

    def get_current_avg_size(self, include_ramp=True):
        current_size = math.ceil(self.current_val.mean().item() * self._max_size)
        if include_ramp:
            current_size += self._ramp_size
        current_size = max(0, min(self._max_size, current_size))
        return current_size

    def clamp_param(self):
        """this need to be called after each update"""
        self.current_val.data.clamp_(0, 1)





class AttentionConv(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0, groups=1, bias=False,
                 R=3, z_init=4, image_size=32, adaptive_span=False):

        super(AttentionConv, self).__init__()
        self.out_channels = out_channels

        # Note– we will assume a kernel_size x kernel_size context window for applying attention.
        # TODO: Debug: may want to use relative cosine embeddings instead so param count not so
        #               large when kernel size is large in adaptive span.
        # Reason for large kernel_size is that need relative embeddings to be large enough if our
        # model ends up wanting to attend to very large kernels. (easier if this is odd number later on)
        self.kernel_size = image_size+1 if adaptive_span else kernel_size
        self.adaptive_span = adaptive_span
        self.stride = stride
        self.padding = padding
        self.groups = groups

        assert self.out_channels % self.groups == 0, "out_channels should be divided by groups. (example: out_channels: 40, groups: 4)"

        # Note the different usage of kernel_size for rel_w and rel_h. They are 2 one dimensional arrays
        # Reason they divide by two is that in the paper they just concat rel_h and rel_w to be the positional
        # embedding vector
        self.rel_h = nn.Parameter(torch.randn(out_channels // 2, 1, 1, kernel_size, 1), requires_grad=True)
        self.rel_w = nn.Parameter(torch.randn(out_channels // 2, 1, 1, 1, kernel_size), requires_grad=True)

        max_mask_size = image_size/2
        self.adaptive_mask = AdaptiveMask(max_mask_size, R, init_val=z_init,
                                         shape=(groups,1))

        self.key_conv = nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=bias)
        self.query_conv = nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=bias)
        self.value_conv = nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=bias)

        self.reset_parameters()

    def forward(self, x):

        '''
        Comment on 2d masking:
        Have defined z and R as follows in this 2D case:
        z: if have center pixel in kernel, then z is number of pixels to the right of this center pixel that
            get a mask of 1, NOTE: This is a float (ie can be say 2.5 pixels away from center).
            An example is if have 3x3 kernel which should not be masked, then z=2

        R: is buffer around the non masked kernel to give soft masking which makes this differentiable (ie ramplength).
            For ex: If R=1, and we want a 3x3 kernel to not be masked, then anything outside the 4x4 kernel
            gets attention weight of 0.

        Based on mask, we choose min kernel size we need to compute, and then pad x accordingly. To keep shape,
        we just need padding=(kernel_size-1)/2 so need to choose kernel_size to be odd.

        TODO: Add L1 regularizer for masking vars

        When we add relative embeddings, look from center of rel_h and rel_w outwards to do this properly.
        '''

        batch, channels, height, width = x.size()

        if self.adaptive_span:
            kernel_size = math.ceil(
                2 * (self.adaptive_mask.get_current_max_size()+1) ) # compute smallest kernel_size we can compute based on mask
            if kernel_size % 2 == 0:
                kernel_size += 1

            padding = int((kernel_size - 1) / 2)

        else:
            kernel_size = self.kernel_size
            padding = self.padding

        padded_x = F.pad(x, [padding, padding, padding, padding])
        # the query should come from the pixel under consideration, while the keys and values should come from the
        # context window
        q_out = self.query_conv(x)
        k_out = self.key_conv(padded_x)
        v_out = self.value_conv(padded_x)

        k_out = k_out.unfold(2, kernel_size, self.stride).unfold(3, kernel_size, self.stride)
        v_out = v_out.unfold(2, kernel_size, self.stride).unfold(3, kernel_size, self.stride)
        #now k_out has shape (bsz, out_channels, height, width,3,3) where kernel =(3,3) (so has keys for each block which makes it easy to apply attention)

        #now we add relative height to the first half of the output channels and relative width to the second half
        if self.adaptive_span:
            # now we add relative height to the first half of the output channels and relative width to the second half
            # Index these relatives based on kernel size (we know kernel size is odd and length of self.rel_h is odd
            # so can start at center element of self.rel_h and work outwards in both directions equally until
            # using kernel_size elements.
            # TODO Debug: Ensure correctness of this indexing
            start_ind = (self.kernel_size // 2) - (kernel_size // 2)  # remember self.kernel_size != kernel_size
            end_ind = (self.kernel_size // 2) + (kernel_size // 2)
            rel_h = self.rel_h[:, :, :, start_ind:end_ind + 1, :]
            rel_w = self.rel_w[:, :, :, :, start_ind:end_ind + 1]
        else:
            rel_h = self.rel_h
            rel_w = self.rel_w

        k_out_h, k_out_w = k_out.split(self.out_channels // 2, dim=1)
        k_out = torch.cat((k_out_h + rel_h, k_out_w + rel_w), dim=1)

        #for now suppose groups is 1, RETHINK THIS IF NOT
        #this operation just flattens the kernels in the last two dimensions (does this properly from example I did)
        k_out = k_out.contiguous().view(batch, self.groups, self.out_channels // self.groups, height, width, -1)
        v_out = v_out.contiguous().view(batch, self.groups, self.out_channels // self.groups, height, width, -1)

        q_out = q_out.view(batch, self.groups, self.out_channels // self.groups, height, width, 1)

        #DONT UNDERSTAND WHY HE MULTIPLIED LIKE THIS, HIS IS COMMENTED OUT
        #out = q_out * k_out
        #out = F.softmax(out, dim=-1)
        #out = torch.einsum('bnchwk,bnchwk -> bnchw', out, v_out).view(batch, -1, height, width)

        #I think way to do this is is multiply (broadcast over last dimension) then sum dim=2 (acts as dot product)
        #TO DO: Check that this still works with groups > 1 (I think may need to do a flattening after in this case
        out = (q_out*k_out).sum(dim=2)
        out2 = F.softmax(out, dim=-1)
        out3 = (out2.unsqueeze(dim=2) * v_out).sum(dim=-1).squeeze(dim=1) #Check if can condense this in one einstein

        out3 = self.adaptive_mask(out3)

        return out3


    def reset_parameters(self):
        init.kaiming_normal_(self.key_conv.weight, mode='fan_out', nonlinearity='relu')
        init.kaiming_normal_(self.value_conv.weight, mode='fan_out', nonlinearity='relu')
        init.kaiming_normal_(self.query_conv.weight, mode='fan_out', nonlinearity='relu')

        init.normal_(self.rel_h, 0, 1)
        init.normal_(self.rel_w, 0, 1)


if __name__ == '__main__':
    temp = torch.randn((2, 3, 32, 32))
    conv = AttentionConv(3, 16, kernel_size=3, padding=1)
    print(conv(temp).size())
