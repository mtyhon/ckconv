import torch
import torch.fft
import torch.nn
import ckconv
from ckconv.nn.misc import Multiply
import numpy as np
import ckconv.nn.functional as ckconv_f
from torch.nn.utils import weight_norm
from math import pi, sqrt, exp


class KernelNet(torch.nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        hidden_channels: int,
        activation_function: str,
        norm_type: str,
        dim_linear: int,
        bias: bool,
        omega_0: float,
        weight_dropout: float,
    ):
        """
        Creates an 3-layer MLP, which parameterizes a convolutional kernel as:

        relative positions -> hidden_channels -> hidden_channels -> in_channels * out_channels


        :param in_channels:  Dimensionality of the relative positions (Default: 1).
        :param out_channels:  input channels * output channels of the resulting convolutional kernel.
        :param hidden_channels: Number of hidden units.
        :param activation_function: Activation function used.
        :param norm_type: Normalization type used.
        :param dim_linear:  Spatial dimension of the input, e.g., for audio = 1, images = 2 (only 1 suported).
        :param bias:  If True, adds a learnable bias to the layers.
        :param omega_0: Value of the omega_0 value (only used in Sine networks).
        :param weight_dropout: Dropout rate applied to the sampled convolutional kernel.
        """
        super().__init__()

        is_siren = activation_function == "Sine"
        w_dp = weight_dropout != 0.0

        Norm = {
            "BatchNorm": torch.nn.BatchNorm1d,
            "LayerNorm": ckconv.nn.LayerNorm,
            "": torch.nn.Identity,
        }[norm_type]
        ActivationFunction = {
            "ReLU": torch.nn.ReLU,
            "LeakyReLU": torch.nn.LeakyReLU,
            "Swish": ckconv.nn.Swish,
            "Sine": ckconv.nn.Sine,
        }[activation_function]
        Linear = {1: ckconv.nn.Linear1d, 2: ckconv.nn.Linear2d}[dim_linear]

        self.kernel_net = torch.nn.Sequential(
            weight_norm(Linear(in_channels, hidden_channels, bias=bias)),
            Multiply(omega_0) if is_siren else torch.nn.Identity(),
            Norm(hidden_channels) if not is_siren else torch.nn.Identity(),
            ActivationFunction(),
            weight_norm(Linear(hidden_channels, hidden_channels, bias=bias)),
            Multiply(omega_0) if is_siren else torch.nn.Identity(),
            Norm(hidden_channels) if not is_siren else torch.nn.Identity(),
            ActivationFunction(),
            weight_norm(Linear(hidden_channels, out_channels, bias=bias)),
            torch.nn.Dropout(p=weight_dropout) if w_dp else torch.nn.Identity(),
        )

        # initialize the kernel function
        self.initialize(
            mean=0.0,
            variance=0.01,
            bias_value=0.0,
            is_siren=(activation_function == "Sine"),
            omega_0=omega_0,
        )

    def forward(self, x):
        return self.kernel_net(x)

    def initialize(self, mean, variance, bias_value, is_siren, omega_0):

        if is_siren:
            # Initialization of SIRENs
            net_layer = 1
            for (i, m) in enumerate(self.modules()):
                if (
                    isinstance(m, torch.nn.Conv1d)
                    or isinstance(m, torch.nn.Conv2d)
                    or isinstance(m, torch.nn.Linear)
                ):
                    if net_layer == 1:
                        m.weight.data.uniform_(
                            -1, 1
                        )  # Normally (-1, 1) / in_dim but we only use 1D inputs.
                        # Important! Bias is not defined in original SIREN implementation!
                        net_layer += 1
                    else:
                        m.weight.data.uniform_(
                            -np.sqrt(6.0 / m.weight.shape[1]) / omega_0,
                            # the in_size is dim 2 in the weights of Linear and Conv layers
                            np.sqrt(6.0 / m.weight.shape[1]) / omega_0,
                        )
                    # Important! Bias is not defined in original SIREN implementation
                    if m.bias is not None:
                        m.bias.data.uniform_(-1.0, 1.0)
        else:
            # Initialization of ReLUs
            net_layer = 1
            intermediate_response = None
            for (i, m) in enumerate(self.modules()):
                if (
                    isinstance(m, torch.nn.Conv1d)
                    or isinstance(m, torch.nn.Conv2d)
                    or isinstance(m, torch.nn.Linear)
                ):
                    m.weight.data.normal_(
                        mean,
                        variance,
                    )
                    if m.bias is not None:

                        if net_layer == 1:
                            # m.bias.data.fill_(bias_value)
                            range = torch.linspace(-1.0, 1.0, steps=m.weight.shape[0])
                            bias = -range * m.weight.data.clone().squeeze()
                            m.bias = torch.nn.Parameter(bias)

                            intermediate_response = [
                                m.weight.data.clone(),
                                m.bias.data.clone(),
                            ]
                            net_layer += 1

                        elif net_layer == 2:
                            range = torch.linspace(-1.0, 1.0, steps=m.weight.shape[0])
                            range = range + (range[1] - range[0])
                            range = (
                                range * intermediate_response[0].squeeze()
                                + intermediate_response[1]
                            )

                            bias = -torch.einsum(
                                "oi, i -> o", m.weight.data.clone().squeeze(), range
                            )
                            m.bias = torch.nn.Parameter(bias)

                            net_layer += 1

                        else:
                            m.bias.data.fill_(bias_value)


class CKConv(torch.nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        hidden_channels: int,
        activation_function: str,
        norm_type: str,
        dim_linear: int,
        bias: bool,
        omega_0: float,
        weight_dropout: float,
    ):
        """
        Creates a Continuous Kernel Convolution.

        :param in_channels: Number of channels in the input signal
        :param out_channels: Number of channels produced by the convolution
        :param hidden_channels: Number of hidden units in the network parameterizing the ConvKernel (KernelNet).
        :param activation_function: Activation function used in KernelNet.
        :param norm_type: Normalization type used in KernelNet. (only for non-Sine KernelNets).
        :param dim_linear: patial dimension of the input, e.g., for audio = 1, images = 2 (only 1 suported).
        :param bias: If True, adds a learnable bias to the output.
        :param omega_0: Value of the omega_0 value of the KernelNet. (only for non-Sine KernelNets).
        :param weight_dropout: Dropout rate applied to the sampled convolutional kernels.
        """
        super().__init__()
        self.Kernel = KernelNet(
            dim_linear,
            out_channels * in_channels,
            hidden_channels,
            activation_function,
            norm_type,
            dim_linear,
            bias,
            omega_0,
            weight_dropout,
        )

        if bias:
            self.bias = torch.nn.Parameter(torch.Tensor(out_channels))
            self.bias.data.fill_(value=0.0)
        else:
            self.bias = None

        # Non-persistent values
        self.rel_positions = None
        self.sigma = None
        self.sr_change = 1.0

        self.register_buffer("train_length", torch.zeros(1).int(), persistent=True)
        self.register_buffer("conv_kernel", torch.zeros(in_channels), persistent=False)

    def forward(self, x):
        # Construct kernel
        x_shape = x.shape

        rel_pos = self.handle_rel_positions(x)
        conv_kernel = self.Kernel(rel_pos).view(-1, x_shape[1], *x_shape[2:])

        # ---- Different samling rate --------
        # If freq test > freq test, smooth out high-freq elements.
        if self.sigma is not None:
            with torch.no_grad():
                n = int(1 / self.sr_change) * 2 + 1
                h = max(1, n // 2)
                G = (
                    lambda x: 1
                    / (self.sigma * sqrt(2 * pi))
                    * exp(-float(x) ** 2 / (2 * self.sigma ** 2))
                )
    
                smoothing_ker = [G(x) for x in range(-h, h + 1)]
                unsq = torch.Tensor(smoothing_ker).cuda().unsqueeze(0).unsqueeze(0).clone()
    #             print('Smoothing Ker Size: ', unsq.size())
                conv_kernel_clone = conv_kernel.clone()
                conv_smoothing = torch.conv1d(
                    conv_kernel_clone.view(-1, 1, *x_shape[2:]), unsq, padding=0
                )
#                 print('Conv Smoothing size: ', conv_smoothing.size())
#                 print('h: ', n//2)
#                 print('Conv Kernel Ori Size: ', conv_kernel_clone.size())
#                 print('Conv Kernel hh size: ', conv_kernel_clone[:, :, h:-h].size())
#                 print('Conv Smoothing View size: ', conv_smoothing.view(*conv_kernel_clone.shape[:-1], -1).size())

                conv_kernel_clone[:, :, h:-h] = conv_smoothing.view(*conv_kernel_clone.shape[:-1], -1)
                conv_kernel = conv_kernel_clone
        # multiply by the sr_train / sr_test
        if self.sr_change != 1.0:
            conv_kernel = conv_kernel * self.sr_change
        # ------------------------------------

        # For computation of "weight_decay"
        self.conv_kernel = conv_kernel

        # We have noticed that the results of fftconv become very noisy when the length of
        # the input is very small ( < 50 samples). As this might occur when we use subsampling,
        # we replace causal_fftconv by causal_conv in settings where this occurs.
        if x_shape[-1] < self.train_length.item():
            # Use spatial convolution:
            return ckconv_f.causal_conv(x, conv_kernel, self.bias)
        else:
            # Otherwise use fft convolution:
            return ckconv_f.causal_fftconv(x, conv_kernel, self.bias)

    def handle_rel_positions(self, x):
        """
        Handles the vector or relative positions which is given to KernelNet.
        """
        if self.rel_positions is None:
            if self.train_length[0] == 0:
                # The ckconv has not been trained yet. Set maximum length to be 1.
                self.train_length[0] = x.shape[-1]

            # Calculate the maximum relative position based on the length of the train set,
            # and the current length of the input.
            max_relative_pos = self.calculate_max(
                self.train_length.item(), current_length=x.shape[-1]
            )

            # Creates the vector of relative positions.
            self.rel_positions = (
                torch.linspace(-1.0, max_relative_pos, x.shape[-1])
                .cuda()
                .unsqueeze(0)
                .unsqueeze(0)
            )  # -> With form: [batch_size=1, in_channels=1, x_dimension]

            # calculate and save the sr ratio for later
            if self.train_length.item() > x.shape[-1]:
                self.sr_change = round(self.train_length.item() / x.shape[-1])
            else:
                self.sr_change = 1 / round(x.shape[-1] / self.train_length.item())

            # if new signal has higher frequency
            if self.sr_change < 1:
                self.sigma = 0.5
            else: # don't blur
                self.sigma = None 

        return self.rel_positions

    @staticmethod
    def calculate_max(
        train_length: int,
        current_length: int,
    ) -> float:
        """
        Calculates the maximum relative position for the current length based on the input length.
        This is used to avoid kernel misalignment (see Appx. D.2).

        :param train_length: Input length during training.
        :param current_length: Current input length.
        :return: Returns the max relative position for the calculation of the relative
                 positions vector. The max. of train is always equal to 1.
        """
        # get sampling rate ratio
        if train_length > current_length:
            sr_change = round(train_length / current_length)
        else:
            sr_change = 1 / round(current_length / train_length)

        # get step sizes (The third parameter of torch.linspace).
        train_step = 2.0 / (train_length - 1)
        current_step = train_step * sr_change

        # Calculate the maximum relative position.
        if sr_change > 1:
            substract = (train_length - 1) % sr_change
            max_relative_pos = 1 - substract * train_step
        else:
            add = (current_length - 1) % (1 / sr_change)
            max_relative_pos = 1 + add * current_step
        return max_relative_pos
