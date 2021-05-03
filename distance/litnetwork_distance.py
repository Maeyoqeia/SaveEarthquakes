import pytorch_lightning as pl
import torch.nn
from pytorch_lightning.core.lightning import LightningModule
from torch import Tensor
from torch.nn import Linear, ReLU, Flatten, Sequential, Conv1d, MaxPool1d, BatchNorm1d  # GaussianNLLLoss
from torch.nn.modules.loss import _Loss


class GaussianNLLLoss(_Loss):
    __constants__ = ['full', 'eps', 'reduction']
    full: bool
    eps: float

    def __init__(self, *, full: bool = False, eps: float = 1e-6, reduction: str = 'mean') -> None:
        super(GaussianNLLLoss, self).__init__(None, None, reduction)
        self.full = full
        self.eps = eps

    def forward(self, input: Tensor, target: Tensor, var: Tensor) -> Tensor:
        return gaussian_nll_loss(input, target, var, full=self.full, eps=self.eps, reduction=self.reduction)


def gaussian_nll_loss(input, target, var, *, full=False, eps=1e-6, reduction='mean'):
    r"""Gaussian negative log likelihood loss.

    See :class:`~torch.nn.GaussianNLLLoss` for details.

    Args:
        input: expectation of the Gaussian distribution.
        target: sample from the Gaussian distribution.
        var: tensor of positive variance(s), one for each of the expectations
            in the input (heteroscedastic), or a single one (homoscedastic).
        full: ``True``/``False`` (bool), include the constant term in the loss
            calculation. Default: ``False``.
        eps: value added to var, for stability. Default: 1e-6.
        reduction: specifies the reduction to apply to the output:
            `'none'`` | ``'mean'`` | ``'sum'``. ``'none'``: no reduction will be applied,
            ``'mean'``: the output is the average of all batch member losses,
            ``'sum'``: the output is the sum of all batch member losses.
            Default: ``'mean'``.
    """
    if not torch.jit.is_scripting():
        tens_ops = (input, target, var)
        if any([type(t) is not Tensor for t in tens_ops]) and has_torch_function(tens_ops):
            return handle_torch_function(
                gaussian_nll_loss, tens_ops, input, target, var, full=full, eps=eps, reduction=reduction)

    # Inputs and targets much have same shape
    input = input.view(input.size(0), -1)
    target = target.view(target.size(0), -1)
    if input.size() != target.size():
        raise ValueError("input and target must have same size")

    # Second dim of var must match that of input or be equal to 1
    var = var.view(input.size(0), -1)
    if var.size(1) != input.size(1) and var.size(1) != 1:
        raise ValueError("var is of incorrect size")

    # Check validity of reduction mode
    if reduction != 'none' and reduction != 'mean' and reduction != 'sum':
        raise ValueError(reduction + " is not valid")

    # Entries of var must be non-negative
    if torch.any(var < 0):
        raise ValueError("var has negative entry/entries")

    # Clamp for stability
    var = var.clone()
    with torch.no_grad():
        var.clamp_(min=eps)

    # Calculate loss (without constant)
    loss = 0.5 * (torch.log(var) + (input - target) ** 2 / var).view(input.size(0), -1).sum(dim=1)

    # Add constant to loss term if required
    if full:
        D = input.size(1)
        loss = loss + 0.5 * D * math.log(2 * math.pi)

    # Apply reduction
    if reduction == 'mean':
        return loss.mean()
    elif reduction == 'sum':
        return loss.sum()
    else:
        return loss


class LitNetwork(LightningModule):
    def __init__(self):
        super().__init__()
        self.cnn_layer1 = Sequential(
            Conv1d(3, 32, kernel_size=21, stride=1, padding=10),
            # pytorch hat noch keine padding_mode = same implementation
            BatchNorm1d(
                32
            ),  # batch norm values taken from keras default
            ReLU(),
            MaxPool1d(kernel_size=2, stride=2),
        )
        self.cnn_layer2 = Sequential(
            Conv1d(32, 64, kernel_size=15, stride=1, padding=7),
            BatchNorm1d(64),
            ReLU(),
            MaxPool1d(kernel_size=2, stride=2),
        )
        self.cnn_layer3 = Sequential(
            Conv1d(64, 128, kernel_size=11, stride=1, padding=5),
            BatchNorm1d(128),
            ReLU(),
            MaxPool1d(kernel_size=2, stride=2),
        )
        self.cnn_layer4 = Sequential(
            Conv1d(128, 256, kernel_size=9, stride=1, padding=4),
            BatchNorm1d(256),
            ReLU(),
            MaxPool1d(kernel_size=2, stride=2),
            Flatten(),
        )

        self.linear_layers1 = Sequential(
            Linear(25 * 256 * 5, 200),  # the keras network uses 200 units, so...
            BatchNorm1d(200),
            ReLU(),
        )
        self.linear_layers2 = Sequential(
            Linear(200, 2),
            BatchNorm1d(2),
            ReLU(),
        )

        self.test_acc = pl.metrics.Accuracy()
        self.train_acc = pl.metrics.Accuracy()
        self.val_acc = pl.metrics.Accuracy()

    def forward(self, x):
        x = self.cnn_layer1(x)
        x = self.cnn_layer2(x)
        x = self.cnn_layer3(x)
        x = self.cnn_layer4(x)
        x = self.linear_layers1(x)
        x = self.linear_layers2(x)
        return x

    def training_step(self, inputs, inputs_idx):
        waveform = inputs["waveform"]
        label = inputs["label"]
        outputs = self(waveform)
        learned = outputs[:, 0]
        var = outputs.data[:, 1]
        criterion = GaussianNLLLoss()
        loss = criterion(learned, label, var)
        self.log("train_loss", loss)
        return loss

    def validation_step(self, inputs, inputs_idx):
        waveform = inputs["waveform"]
        label = inputs["label"]
        outputs = self(waveform)
        learned = outputs[:, 0]
        var = outputs.data[:, 1]
        criterion = GaussianNLLLoss()
        loss = criterion(learned, label, var)
        self.log("val_loss", loss)
        return loss

    def test_step(self, inputs, inputs_idx):
        waveform = inputs["waveform"]
        label = inputs["label"]
        outputs = self(waveform)
        learned = outputs[:, 0]
        var = outputs.data[:, 1]
        criterion = GaussianNLLLoss()
        loss = criterion(learned, label, var)
        self.log("test_loss", loss)
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters())
        return optimizer
