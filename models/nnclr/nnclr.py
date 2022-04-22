import logging
from typing import Tuple

import lightly.loss as loss
import torch
import torchvision
from lightly.models.modules import NNCLRProjectionHead, NNCLRPredictionHead, NNMemoryBankModule
from torch import nn
from torch.utils import data as torch_data

from configuration.configuration import Configuration


def get_convnext():
    """
    Returns a ConvNeXt tiny backbone with only one channel instead of three as input
    :return: ConvNeXt
    """
    # Use an convnext_tiny backbone
    backbone = torchvision.models.convnext_tiny(pretrained=True)  # pretrained model is loaded
    backbone.features[0][0] = nn.Conv2d(1, 96, (4, 4), (4, 4))
    backbone = nn.Sequential(*list(backbone.children())[:-1])  # remove classification layer

    return backbone


NAME_PREFIX = "nnclr"


class NNCLR(nn.Module):
    """
    NNCRL model
    """

    def __init__(self, backbone: nn.Sequential,
                 num_ftrs: int = 768,  # 1536
                 proj_hidden_dim: int = 768,
                 pred_hidden_dim: int = 768,
                 out_dim: int = 512,
                 freeze_layers: int = False):
        super().__init__()

        self.backbone = backbone
        self.projection_head = NNCLRProjectionHead(num_ftrs, proj_hidden_dim, out_dim)
        self.prediction_head = NNCLRPredictionHead(out_dim, pred_hidden_dim, out_dim)
        self.memory_bank = NNMemoryBankModule(size=8192)  # the size of support set

        # use a criterion for self-supervised learning
        self.criterion = loss.NTXentLoss(temperature=0.1)

        # get a PyTorch optimizer
        self.optimizer = torch.optim.SGD(self.parameters(), lr=0.06, weight_decay=1e-5)

        if freeze_layers > 0:
            self.freeze_layers(last_mbconv_blocks=freeze_layers)

        logging.info("# trainable parameters: {}".format(sum(p.numel() for p in self.parameters() if p.requires_grad)))
        self.name = ""

    def set_name(self, seed, conf_id):
        self.name = self.get_name_as_string(seed, conf_id)

    @staticmethod
    def get_name_as_string(seed, conf_id):
        return "{}_seed-{}_conf_id-{}".format(NAME_PREFIX, seed, conf_id)

    def freeze_layers(self, last_mbconv_blocks: int = 2):
        """
        Sets "requires_grad" to False of the given MBConv blocks.
        :param last_mbconv_blocks: the last MBConv blocks that will not be trained
        """
        model_children = list(self.backbone.children())
        for idx, child in enumerate(model_children):
            if isinstance(child, nn.Sequential):
                for sub_idx, sub_child in enumerate(list(child)):
                    if sub_idx not in list(range(len(list(child)) - last_mbconv_blocks, len(list(child)))):
                        logging.info("\nThe following modules will not be trained: {}".format(sub_child.modules))
                        for param in sub_child.parameters():
                            param.requires_grad = False
            elif isinstance(child, nn.AdaptiveAvgPool2d):
                logging.info("\nThe following modules will not be trained: {}".format(child.modules))
                for param in child.parameters():
                    param.requires_grad = False

        logging.info(
            "# non-trainable parameters: {}".format(
                sum(p.numel() for p in self.backbone.parameters() if ~ p.requires_grad)))

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        y = self.backbone(x).flatten(start_dim=1)
        z = self.projection_head(y)
        p = self.prediction_head(z)
        z = z.detach()
        return z, p

    def save(self, file_path: str) -> None:
        """
        Save a model
        :param file_path: a path to a folder
        :param epoch: epoch
        """
        backbone_state_dict = self.backbone.state_dict()
        projection_mlp_state_dict = self.projection_head.state_dict()
        prediction_mlp_state_dict = self.prediction_head.state_dict()
        out = "{}{}".format(file_path, self.name)
        torch.save({"backbone": backbone_state_dict,
                    "projection": projection_mlp_state_dict,
                    "prediction": prediction_mlp_state_dict},
                   out)
        logging.info("Checkpoint: {} is saved".format(out))

    def load(self, file_path: str) -> None:
        """
        Load a saved model
        :param file_path: a path to a file
        :return:
        """
        checkpoint = torch.load(file_path)
        self.backbone.load_state_dict(checkpoint["backbone"])
        self.projection_head.load_state_dict(checkpoint["projection"])
        self.prediction_head.load_state_dict(checkpoint["prediction"])
        logging.info("Checkpoint: {} is loaded".format(str(file_path)))

    @staticmethod
    def load_state_dict_(feature_extractor: nn.Sequential, checkpoint: str) -> nn.Sequential:
        """
        Load a state dictionary
        :param feature_extractor: a backbone
        :param checkpoint: a path to a model
        :return: a model with loaded state dictionary
        """

        if len(checkpoint) == 0:
            return feature_extractor

        checkpoint_ = torch.load(checkpoint)
        state_dict = checkpoint_['backbone']
        feature_extractor.load_state_dict(state_dict)
        logging.info("Checkpoint: {} is loaded".format(str(checkpoint)))

        return feature_extractor

    def train_(self, configuration: Configuration, data_loader: torch_data.DataLoader) -> None:
        """
        Train the NNCLR model
        :param configuration: Configuration
        :param data_loader: DataLoader
        """
        logging.info("NNCLR Training ...")
        for epoch in range(1, configuration.nnclr_conf.epochs + 1):
            total_loss = 0

            for idx, (view_one, view_two, _) in enumerate(data_loader):
                # forward pass
                view_one = view_one.to(configuration.device)
                view_two = view_two.to(configuration.device)

                # z and p are projections and predictions heads respectively
                z1, p1 = self(view_one)
                z2, p2 = self(view_two)

                nn1 = self.memory_bank(z1.detach(), update=False)  # top-1 NN lookup without update
                nn2 = self.memory_bank(z2.detach(), update=True)  # top-1 NN lookup with update

                loss = 0.5 * (self.criterion(nn1, p2) + self.criterion(nn2, p1))
                total_loss += loss.detach()
                loss.backward()
                self.optimizer.step()
                self.optimizer.zero_grad()
                if configuration.dry_run:
                    self.save(configuration.nnclr_conf.checkpoint_folder)
                    return
            if epoch % configuration.nnclr_conf.save_nepoch == 0:
                self.save(configuration.nnclr_conf.checkpoint_folder)
            avg_loss = total_loss / len(data_loader)
            logging.info(f"epoch: |{epoch:>02}|, loss: |{avg_loss:.5f}|")
