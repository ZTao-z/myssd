import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from layers import *
from data import voc, coco, custom
import os

from netModel.resnet import resnet34, BasicBlock


class SSD(nn.Module):
    """Single Shot Multibox Architecture
    The network is composed of a base VGG network followed by the
    added multibox conv layers.  Each multibox layer branches into
        1) conv2d for class conf scores
        2) conv2d for localization predictions
        3) associated priorbox layer to produce default bounding
           boxes specific to the layer's feature map size.
    See: https://arxiv.org/pdf/1512.02325.pdf for more details.

    Args:
        phase: (string) Can be "test" or "train"
        size: input image size
        base: resnet layers for input, size of either 300 or 500
        extras: extra layers that feed to multibox loc and conf layers
        head: "multibox head" consists of loc and conf conv layers
    """

    def __init__(self, phase, size, base, extras, head, num_classes):
        super(SSD, self).__init__()
        self.phase = phase
        self.num_classes = num_classes
        self.cfg = (voc, custom)[num_classes == 2]
        self.priorbox = PriorBox(self.cfg)
        self.priors = Variable(self.priorbox.forward(), volatile=True)
        self.size = size

        # SSD network
        self.resnet = nn.ModuleList(base)
        # Layer learns to scale the l2 normalized features from conv4_3
        self.L2Norm_before_multiflow = L2Norm(256, 20)
        self.L2Norm_after_multiflow = L2Norm(256, 20)
        self.extras = nn.ModuleList(extras)

        self.loc = nn.ModuleList(head[0])
        self.conf = nn.ModuleList(head[1])

        self.stre_3 = nn.Sequential(
            nn.Conv2d(256, 256, kernel_size=1, stride=1),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(256, 256, kernel_size=1, stride=1),
            nn.BatchNorm2d(256)
        )
        self.convT_1_3 = nn.Sequential(
            nn.ConvTranspose2d(256, 256, kernel_size=3, stride=1, padding=0, output_padding=0),
            nn.BatchNorm2d(256),
            nn.Conv2d(256, 256, kernel_size=(1, 1), stride=(1, 1)),
            nn.BatchNorm2d(256)
        )
        self.relu_3 = nn.ReLU(inplace=True)

        self.stre_5 = nn.Sequential(
            nn.Conv2d(256, 256, kernel_size=1, stride=1),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(256, 256, kernel_size=1, stride=1),
            nn.BatchNorm2d(256)
        )
        self.convT_3_5 = nn.Sequential(
            nn.ConvTranspose2d(256, 256, kernel_size=3, stride=2, padding=1, output_padding=0),
            nn.BatchNorm2d(256),
            nn.Conv2d(256, 256, kernel_size=(1, 1), stride=(1, 1)),
            nn.BatchNorm2d(256),
        )
        self.relu_5 = nn.ReLU(inplace=True)

        self.stre_10 = nn.Sequential(
            nn.Conv2d(512, 512, kernel_size=1, stride=1),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, kernel_size=1, stride=1),
            nn.BatchNorm2d(512)
        )
        self.convT_5_10 = nn.Sequential(
            nn.ConvTranspose2d(256, 512, kernel_size=3, stride=2, padding=1, output_padding=1),
            nn.BatchNorm2d(512),
            nn.Conv2d(512, 512, kernel_size=(1, 1), stride=(1, 1)),
            nn.BatchNorm2d(512)
        )
        self.relu_10 = nn.ReLU(inplace=True)

        self.stre_19 = nn.Sequential(
            nn.Conv2d(512, 512, kernel_size=1, stride=1),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, kernel_size=1, stride=1),
            nn.BatchNorm2d(512)
        )
        self.convT_10_19 = nn.Sequential(
            nn.ConvTranspose2d(512, 512, kernel_size=3, stride=2, padding=1, output_padding=0),
            nn.BatchNorm2d(512),
            nn.Conv2d(512, 512, kernel_size=(1, 1), stride=(1, 1)),
            nn.BatchNorm2d(512)
        )
        self.relu_19 = nn.ReLU(inplace=True)

        self.stre_38 = nn.Sequential(
            nn.Conv2d(256, 256, kernel_size=1, stride=1),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(256, 256, kernel_size=1, stride=1),
            nn.BatchNorm2d(256)
        )
        self.convT_19_38 = nn.Sequential(
            nn.ConvTranspose2d(512, 256, kernel_size=3, stride=2, padding=1, output_padding=1),
            nn.BatchNorm2d(256),
            nn.Conv2d(256, 256, kernel_size=(1, 1), stride=(1, 1)),
            nn.BatchNorm2d(256)
        )
        self.relu_38 = nn.ReLU(inplace=True)

        self.multiflow_o = nn.Sequential(
            nn.Conv2d(256, 1024, kernel_size=1),
            nn.BatchNorm2d(1024),
            nn.ReLU(inplace=True)
        )
        self.multiflow_d2 = self._make_multiflow_subnetwork(inputs=256, dilation=2)
        self.multiflow_d4 = self._make_multiflow_subnetwork(inputs=256, dilation=4)
        self.multiflow_concat = nn.Sequential(
            nn.Conv2d(1280, 512, kernel_size=1),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 256, kernel_size=1),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True)
        )
        if phase == 'test':
            self.softmax = nn.Softmax(dim=-1)
            self.detect = Detect(num_classes, 0, 200, 0.01, 0.45)
    
    def _make_multiflow_subnetwork(self, inputs, dilation=1):
        layers = [
            nn.Conv2d(inputs, 512, kernel_size=3, padding=dilation, dilation=dilation),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, kernel_size=1),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, kernel_size=3, padding=1, groups=2),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 128, kernel_size=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True)
        ]
        return nn.Sequential(*layers)

    def forward(self, x):
        """Applies network layers and ops on input image(s) x.

        Args:
            x: input image or batch of images. Shape: [batch,3,300,300].

        Return:
            Depending on phase:
            test:
                Variable(tensor) of output class label predictions,
                confidence score, and corresponding location predictions for
                each object detected. Shape: [batch,topk,7]

            train:
                list of concat outputs from:
                    1: confidence layers, Shape: [batch*num_priors,num_classes]
                    2: localization layers, Shape: [batch,num_priors*4]
                    3: priorbox layers, Shape: [2,num_priors*4]
        """
        sources = list()
        sources_final = list()
        loc = list()
        conf = list()

        # apply resnet up to layer2
        for k in range(0,7):
            x = self.resnet[k](x)
        s = self.L2Norm_before_multiflow(x)

        # multiflow_origin
        s0 = s
        s0 = self.multiflow_o(s0)
        # multiflow_dilation2
        s2 = s
        s2 = self.multiflow_d2(s2)
        # multiflow_dilation4
        s4 = s
        s4 = self.multiflow_d4(s4)
        # concat
        s = torch.cat((s0, s2, s4), 1)
        # after_concat
        s = self.multiflow_concat(s)
        x = self.L2Norm_after_multiflow(s)
        sources.append(x)

        # apply resnet up to layer4
        x = self.resnet[7](x)
        sources.append(x)

        # apply extra layers and cache source layer outputs
        for k, v in enumerate(self.extras):
            x = F.relu(v(x), inplace=True)
            if k % 2 == 1:
                sources.append(x)
        temp_x = None
        for k, v in enumerate(reversed(sources)):
            if k == 0:
                sources_final.append(v)
                temp_x = self.convT_1_3(v)
            elif k == 1:
                temp_v = self.relu_3(self.stre_3(v) * temp_x)
                sources_final.append(temp_v)
                temp_x = self.convT_3_5(temp_v)
            elif k == 2:
                temp_v = self.relu_5(self.stre_5(v) * temp_x)
                sources_final.append(temp_v)
                temp_x = self.convT_5_10(temp_v)
            elif k == 3:
                temp_v = self.relu_10(self.stre_10(v) * temp_x)
                sources_final.append(temp_v)
                temp_x = self.convT_10_19(temp_v)
            elif k == 4:
                temp_v = self.relu_19(self.stre_19(v) * temp_x)
                sources_final.append(temp_v)
                temp_x = self.convT_19_38(temp_v)
            else:
                temp_v = self.relu_38(self.stre_38(v) * temp_x)
                sources_final.append(temp_v)
        sources = reversed(sources_final)

        # apply multibox head to source layers
        for (x, l, c) in zip(sources, self.loc, self.conf):
            loc.append(l(x).permute(0, 2, 3, 1).contiguous())
            conf.append(c(x).permute(0, 2, 3, 1).contiguous())

        loc = torch.cat([o.view(o.size(0), -1) for o in loc], 1)
        conf = torch.cat([o.view(o.size(0), -1) for o in conf], 1)
        if self.phase == "test":
            output = self.detect(
                loc.view(loc.size(0), -1, 4),                   # loc preds
                self.softmax(conf.view(conf.size(0), -1,
                             self.num_classes)),                # conf preds
                self.priors.type(type(x.data))                  # default boxes
            )
        else:
            output = (
                loc.view(loc.size(0), -1, 4),
                conf.view(conf.size(0), -1, self.num_classes),
                self.priors
            )
        return output

    def load_weights(self, base_file):
        other, ext = os.path.splitext(base_file)
        if ext == '.pkl' or '.pth':
            print('Loading weights into state dict...')
            self.load_state_dict(torch.load(base_file,
                                 map_location=lambda storage, loc: storage))
            print('Finished!')
        else:
            print('Sorry only .pth and .pkl files supported.')


# This function is derived from torchvision VGG make_layers()
# https://github.com/pytorch/vision/blob/master/torchvision/models/vgg.py
def vgg(cfg, i, batch_norm=False):
    layers = []
    in_channels = i
    for v in cfg:
        if v == 'M':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
        elif v == 'C':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)]
        else:
            conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1)
            if batch_norm:
                layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
            else:
                layers += [conv2d, nn.ReLU(inplace=True)]
            in_channels = v
    pool5 = nn.MaxPool2d(kernel_size=3, stride=1, padding=1)
    conv6 = nn.Conv2d(512, 1024, kernel_size=3, padding=6, dilation=6)
    conv7 = nn.Conv2d(1024, 1024, kernel_size=1)
    layers += [pool5, conv6,
               nn.ReLU(inplace=True), conv7, nn.ReLU(inplace=True)]
    return layers

def resnet():
    resnet = resnet34(pretrained=True)
    layers = [
        resnet.conv1,
        resnet.bn1,
        resnet.relu,
        resnet.maxpool,
        resnet.layer1,
        resnet.layer2,
        resnet.layer3,
        resnet.layer4,
    ]
    return layers

def add_extras(cfg, i, batch_norm=False):
    # Extra layers added to VGG for feature scaling
    layers = []
    in_channels = i
    flag = False
    for k, v in enumerate(cfg):
        if in_channels != 'S':
            if v == 'S':
                layers += [nn.Conv2d(in_channels, cfg[k + 1],
                           kernel_size=(1, 3)[flag], stride=2, padding=1)]
            else:
                layers += [nn.Conv2d(in_channels, v, kernel_size=(1, 3)[flag])]
            flag = not flag
        in_channels = v
    return layers


def multibox(resnet, extra_layers, cfg, num_classes):
    loc_layers = []
    conf_layers = []
    resnet_source = [-2, -1]
    # for k, v in enumerate(resnet_source):
    #     loc_layers += [nn.Conv2d(resnet[v][-1].conv2.out_channels, cfg[k] * 4, kernel_size=3, padding=1)]
    #     conf_layers += [nn.Conv2d(resnet[v][-1].conv2.out_channels, cfg[k] * num_classes, kernel_size=3, padding=1)]
    # for k, v in enumerate(extra_layers[1::2], 2):
    #     loc_layers += [nn.Conv2d(v.out_channels, cfg[k] * 4, kernel_size=3, padding=1)]
    #     conf_layers += [nn.Conv2d(v.out_channels, cfg[k] * num_classes, kernel_size=3, padding=1)]
    for k, v in enumerate(resnet_source):
        loc_layers += [
            nn.Sequential(
                nn.Conv2d(resnet[v][-1].conv2.out_channels, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, cfg[k] * 4, kernel_size=3, padding=1)
            )
        ]
        conf_layers += [
            nn.Sequential(
                nn.Conv2d(resnet[v][-1].conv2.out_channels, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, cfg[k] * num_classes, kernel_size=3, padding=1)
            )
        ]
    for k, v in enumerate(extra_layers[1::2], 2):
        loc_layers += [
            nn.Sequential(
                nn.Conv2d(v.out_channels, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, cfg[k] * 4, kernel_size=3, padding=1)
            )
        ]
        conf_layers += [
            nn.Sequential(
                nn.Conv2d(v.out_channels, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, 256, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(256, cfg[k] * num_classes, kernel_size=3, padding=1)
            )
        ]
    return resnet, extra_layers, (loc_layers, conf_layers)


base = {
    '300': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 'C', 512, 512, 512, 'M',
            512, 512, 512],
    '512': [],
}
extras = {
    '300': [256, 'S', 512, 128, 'S', 256, 128, 256, 128, 256],
    '512': [],
}
mbox = {
    '300': [4, 6, 6, 6, 4, 4],  # number of boxes per feature map location
    '512': [],
}


def build_ssd(phase, size=300, num_classes=21):
    if phase != "test" and phase != "train":
        print("ERROR: Phase: " + phase + " not recognized")
        return
    if size != 300:
        print("ERROR: You specified size " + repr(size) + ". However, " +
              "currently only SSD300 (size=300) is supported!")
        return
    base_, extras_, head_ = multibox(resnet(),
                                     add_extras(extras[str(size)], 512),
                                     mbox[str(size)], num_classes)
    return SSD(phase, size, base_, extras_, head_, num_classes)
