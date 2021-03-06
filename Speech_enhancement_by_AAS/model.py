import math
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
from torch.autograd import Variable

import pdb

supported_rnns = {
    'lstm': nn.LSTM,
    'rnn': nn.RNN,
    'gru': nn.GRU
}
supported_rnns_inv = dict((v, k) for k, v in supported_rnns.items())

class L1Loss_mask(nn.Module):
    def __init__(self):
        super(L1Loss_mask, self).__init__()

    def forward(self, input, target, mask):
        mask_sum = mask.data.sum()
        if(mask.data[0][0][0] == 0): # data_as_0 = True
            nElement = mask.data.nelement() - mask_sum

        err = torch.abs(input-target)
        err.masked_fill(mask, 0)
        loss = err.sum()/nElement
        return loss, nElement


class SequenceWise(nn.Module):
    def __init__(self, module):
        """
        Collapses input of dim T*N*H to (T*N)*H, and applies to a module.
        Allows handling of variable sequence lengths and minibatch sizes.
        :param module: Module to apply input to.
        """
        super(SequenceWise, self).__init__()
        self.module = module

    def forward(self, x):
        t, n = x.size(0), x.size(1)
        x = x.view(t * n, -1)
        x = self.module(x)
        x = x.view(t, n, -1)
        return x

    def __repr__(self):
        tmpstr = self.__class__.__name__ + ' (\n'
        tmpstr += self.module.__repr__()
        tmpstr += ')'
        return tmpstr


class InferenceBatchSoftmax(nn.Module):
    def forward(self, input_):
        if not self.training:
            batch_size = input_.size()[0]
            return torch.stack([F.softmax(input_[i], dim=1) for i in range(batch_size)], 0)
        else:
            return input_

class BatchRNN(nn.Module):
    def __init__(self, input_size, hidden_size, rnn_type=nn.LSTM, bidirectional=False, batch_norm=True):
        super(BatchRNN, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.bidirectional = bidirectional
        self.batch_norm = SequenceWise(nn.BatchNorm1d(input_size)) if batch_norm else None
        self.rnn = rnn_type(input_size=input_size, hidden_size=hidden_size,
                            bidirectional=bidirectional, bias=False)
        self.num_directions = 2 if bidirectional else 1

    def flatten_parameters(self):
        self.rnn.flatten_parameters()

    def forward(self, x):
        if self.batch_norm is not None:
            x = self.batch_norm(x)
        x, _ = self.rnn(x)
        if self.bidirectional:
            x = x.view(x.size(0), x.size(1), 2, -1).sum(2).view(x.size(0), x.size(1), -1)  # (TxNxH*2) -> (TxNxH) by sum
        return x

class BRNN(nn.Module):
    def __init__(self, input_size, hidden_size, rnn_type=nn.LSTM, bidirectional=False):
        super(BRNN, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.bidirectional = bidirectional
        self.rnn = rnn_type(input_size=input_size, hidden_size=hidden_size,
                            bidirectional=bidirectional, bias=False)
        self.num_directions = 2 if bidirectional else 1

    def flatten_parameters(self):
        self.rnn.flatten_parameters()

    def forward(self, x):
        x, _ = self.rnn(x)
        if self.bidirectional:
            x = x.view(x.size(0), x.size(1), 2, -1).sum(2).view(x.size(0), x.size(1), -1)  # (TxNxH*2) -> (TxNxH) by sum
        return x

class SpeechClassifierRNN(nn.Module):
    def __init__(self, I, O, H, L=3, rnn_type=nn.LSTM):
        super(SpeechClassifierRNN, self).__init__()
        self.I = I
        self.H = H
        self.L = L

        self.rnn1 = BRNN(input_size=H, hidden_size = H, rnn_type = rnn_type, bidirectional=True)
        self.rnn2 = BRNN(input_size=H, hidden_size=H, rnn_type=rnn_type, bidirectional=True)
        self.rnn3 = BRNN(input_size=H, hidden_size=H, rnn_type=rnn_type, bidirectional=True)

        if(I != H):
            self.first_linear = nn.Conv1d(I, H, kernel_size=1, stride=1, padding=0) # linear transform from dimension I to H, needed for residual connection
        else:
            self.first_linear = None

        self.final_linear = nn.Linear(H, O)
        self.criterion = nn.CrossEntropyLoss(size_average = False)   # nn.LogSoftmax() + nn.NLLLoss() in one single class

    def forward(self, input, target):
        #pdb.set_trace()
        if(self.first_linear):
            input = self.first_linear(input)
        input = input.transpose(1,2).transpose(0,1) # Transpose: NxHxT --> TxNxH
        h1 = self.rnn1(input) + input
        h2 = self.rnn2(h1) + h1
        h3 = self.rnn3(h2) + h2

        # Ver1
        #h3 = h3.transpose(0,1).transpose(1,2) # Transpose back: TxNxH --> NxHxT

        # Ver2
        h3 = h3.sum(0) # TxNxH --> NxH

        output = self.final_linear(h3) # NxH --> NxO
        loss = self.criterion(output, target)

        return loss


class BRNNmultiCH(nn.Module):
    def __init__(self, I, H, L, nCH, mel_basis, rnn_type=nn.GRU):
        super(BRNNmultiCH, self).__init__()
        self.I = I
        self.H = H
        #self.L = L # currently, fix L=2 (implementation issue)
        self.nCH = nCH
        self.rnn_type = rnn_type
        self.L = L

        self.rnn1 = BRNN(input_size=H, hidden_size = H, rnn_type = rnn_type, bidirectional=True)
        self.rnn2 = BRNN(input_size=H, hidden_size = H, rnn_type = rnn_type, bidirectional=True)
        if (self.L == 3):
            self.rnn3 = BRNN(input_size=H, hidden_size=H, rnn_type=rnn_type, bidirectional=True)
        #pdb.set_trace()

        self.first_linear = nn.Conv1d(I, H, kernel_size=1, stride=1, padding=0) # linear transform from dimension I to H, needed for residual connection
        self.final_linear_real = nn.Conv1d(H, int(I/2), kernel_size=1, stride=1, padding=0) # final linear mask for real (/2 for considering only real part)
        self.final_linear_imag = nn.Conv1d(H, int(I/2), kernel_size=1, stride=1, padding=0)  # final linear mask for imag (/2 for considering only imag part)

        self.mel_basis = Variable(torch.unsqueeze(torch.FloatTensor(mel_basis).repeat(1,self.nCH),-1).cuda()) # 40x(nFFT/2+1)x1

    def forward(self, input):
        #input: (N, nCH*F*2,T) (2: real/img)

        input_linear = self.first_linear(input)
        input_linear = input_linear.transpose(1,2).transpose(0,1) # Transpose: NxHxT --> TxNxH

        h1 = self.rnn1(input_linear) + input_linear
        h2 = self.rnn2(h1) + h1
        if(self.L == 3):
            h3 = self.rnn3(h2) + h2
            h = h3.transpose(0,1).transpose(1,2) # Transpose back: TxNxH --> NxHxT
        else:
            h = h2.transpose(0,1).transpose(1,2) # Transpose back: TxNxH --> NxHxT

        mask_real = self.final_linear_real(h)
        mask_imag = self.final_linear_imag(h)

        stft = input.view(input.size(0), 2, -1, input.size(-1)) # Nx2x(CH*F)xT
        stft_real = stft[:,0] # Nx(CH*F)xT
        stft_imag = stft[:,1] # Nx(CH*F)xT

        #pdb.set_trace()
        enh_real = torch.mul(stft_real, mask_real)
        enh_imag = torch.mul(stft_imag, mask_imag)

        enh_power = torch.pow(enh_real, 2) + torch.pow(enh_imag, 2)

        enh_mel = F.conv1d(enh_power, self.mel_basis)

        output = torch.log1p(enh_mel)

        return output


class stackedBRNN(nn.Module):
    def __init__(self, I, O, H, L, rnn_type=nn.LSTM):
        super(stackedBRNN, self).__init__()
        self.I = I
        self.H = H
        self.L = L
        self.rnn_type = rnn_type

        self.rnn1 = BRNN(input_size=H, hidden_size = H, rnn_type = rnn_type, bidirectional=True)
        self.rnn2 = BRNN(input_size=H, hidden_size=H, rnn_type=rnn_type, bidirectional=True)
        self.rnn3 = BRNN(input_size=H, hidden_size=H, rnn_type=rnn_type, bidirectional=True)
        self.rnn4 = BRNN(input_size=H, hidden_size=H, rnn_type=rnn_type, bidirectional=True)

        self.first_linear = nn.Conv1d(I, H, kernel_size=1, stride=1, padding=0) # linear transform from dimension I to H, needed for residual connection
        self.final_linear = nn.Conv1d(H, O, kernel_size=1, stride=1, padding=0) # linear transform from dimension H to I, needed for final output as logMel spectrogram

    def forward(self, input):
        #pdb.set_trace()
        input_linear = self.first_linear(input)
        input_linear = input_linear.transpose(1,2).transpose(0,1) # Transpose: NxHxT --> TxNxH
        h1 = self.rnn1(input_linear) + input_linear
        h2 = self.rnn2(h1) + h1
        h3 = self.rnn3(h2) + h2
        h4 = self.rnn4(h3) + h3
        h4 = h4.transpose(0,1).transpose(1,2) # Transpose back: TxNxH --> NxHxT
        #pdb.set_trace()
        output = self.final_linear(h4)

        return output

    def forward_paired(self, input, paired):

        input = torch.cat((input, paired), dim=1)
        output = self.forward(input)

        return output

    def forward_with_intermediate_output(self, input):
        #pdb.set_trace()
        input_linear = self.first_linear(input)
        input_linear = input_linear.transpose(1,2).transpose(0,1) # Transpose: NxHxT --> TxNxH
        h1 = self.rnn1(input_linear) + input_linear
        h2 = self.rnn2(h1) + h1
        h3 = self.rnn3(h2) + h2
        h4 = self.rnn4(h3) + h3
        h4 = h4.transpose(0,1).transpose(1,2) # Transpose back: TxNxH --> NxHxT
        #pdb.set_trace()
        output = self.final_linear(h4)

        return [output, h4]



class DeepSpeech(nn.Module):
    def __init__(self, rnn_type=nn.LSTM, labels="abc", rnn_hidden_size=512, rnn_layers=2, bidirectional=True,
                 kernel_sz=11, stride=2, map=256, cnn_layers=2,
                 nFreq=40, nDownsample=1, audio_conf = None):
        super(DeepSpeech, self).__init__()

        # model metadata needed for serialization/deserialization
        self.nFreq = nFreq

        self._version = '0.0.1'

        self._audio_conf = audio_conf # not used

        # RNN
        self.rnn_size = rnn_hidden_size
        self.rnn_layers = rnn_layers
        self.rnn_type = rnn_type
        self.bidirectional = bidirectional

        # CNN
        self.cnn_stride = stride   # use stride for subsampling
        self.cnn_map = map
        self.cnn_kernel = kernel_sz
        self.nDownsample = nDownsample

        self.cnn_layers = cnn_layers

        self._labels = labels


        num_classes = len(self._labels)

        conv_list = []
        conv_list.append(nn.Conv1d(nFreq, map, kernel_size=kernel_sz, stride=stride))
        conv_list.append(nn.BatchNorm1d(map))
        conv_list.append(nn.LeakyReLU(map, inplace=True))

        if(self.nDownsample == 1):
            stride=1

        for x in range(self.cnn_layers - 1):
            conv_list.append(nn.Conv1d(map, map, kernel_size=kernel_sz, stride=stride))
            conv_list.append(nn.BatchNorm1d(map))
            conv_list.append(nn.LeakyReLU(map, inplace=True))

        self.conv = nn.Sequential(*conv_list)

        rnn_input_size = map

        rnns = []
        rnn = BatchRNN(input_size=rnn_input_size, hidden_size=rnn_hidden_size, rnn_type=rnn_type,
                       bidirectional=bidirectional, batch_norm=False)
        rnns.append(('0', rnn))
        for x in range(self.rnn_layers - 1):
            rnn = BatchRNN(input_size=rnn_hidden_size, hidden_size=rnn_hidden_size, rnn_type=rnn_type,
                           bidirectional=bidirectional)
            rnns.append(('%d' % (x + 1), rnn))
        self.rnns = nn.Sequential(OrderedDict(rnns))

        fully_connected = nn.Sequential(
            nn.BatchNorm1d(rnn_hidden_size),
            nn.Linear(rnn_hidden_size, num_classes, bias=False)
        )
        self.fc = nn.Sequential(
            SequenceWise(fully_connected),
        )
        self.inference_softmax = InferenceBatchSoftmax()


    def forward(self, x):
        x = self.conv(x)
        #x = x.transpose(1, 2).transpose(0, 1).contiguous()  # TxNxH
        x = x.transpose(1,2).transpose(0,1)
        x = self.rnns(x)
        x = self.fc(x)
        x = x.transpose(0, 1)

        # identity in training mode, softmax in eval mode
        x = self.inference_softmax(x)
        return x

    @classmethod
    def load_model(cls, path, gpu=-1):
        package = torch.load(path, map_location=lambda storage, loc: storage)
        #pdb.set_trace()
        model = cls(rnn_hidden_size=package['rnn_size'], rnn_layers=package['rnn_layers'], rnn_type=supported_rnns[package['rnn_type']],
                    map=package['cnn_map'], stride = package['cnn_stride'], kernel_sz=package['cnn_kernel'], cnn_layers=package['cnn_layers'],
                    labels=package['labels']
                    )
        # the blacklist parameters are params that were previous erroneously saved by the model
        # care should be taken in future versions that if batch_norm on the first rnn is required
        # that it be named something else
        blacklist = ['rnns.0.batch_norm.module.weight', 'rnns.0.batch_norm.module.bias',
                     'rnns.0.batch_norm.module.running_mean', 'rnns.0.batch_norm.module.running_var']
        for x in blacklist:
            if x in package['state_dict']:
                del package['state_dict'][x]
        model.load_state_dict(package['state_dict'])
        for x in model.rnns:
            x.flatten_parameters()

        if gpu>=0:
            model = model.cuda()
        #if cuda:
#            model = torch.nn.DataParallel(model).cuda()
        return model

    @classmethod
    def load_model_package(cls, package, gpu=-1):
        model = cls(rnn_hidden_size=package['rnn_size'], rnn_layers=package['rnn_layers'],rnn_type=supported_rnns[package['rnn_type']],
                    map=package['cnn_map'], stride = package['cnn_stride'], kernel_sz=package['cnn_kernel'], cnn_layers=package['cnn_layers'],
                    labels=package['labels'],
                    )
        model.load_state_dict(package['state_dict'])
        if(gpu>=0):
            model = model.cuda()
        #if cuda:
#            model = torch.nn.DataParallel(model).cuda()
        return model

    @staticmethod
    def serialize(model, optimizer=None, epoch=None, iteration=None, loss_results=None,
                  cer_results=None, wer_results=None, avg_loss=None, meta=None):
        #model_is_cuda = next(model.parameters()).is_cuda
        #pdb.set_trace()
        #model = model.module if model_is_cuda else model
        #model = model._modules if model_is_cuda else model

        package = {
            'version': model._version,
            'rnn_size': model.rnn_size,
            'rnn_layers': model.rnn_layers,
            'cnn_map': model.cnn_map,
            'cnn_kernel': model.cnn_kernel,
            'cnn_stride': model.cnn_stride,
            'cnn_layers': model.cnn_layers,
            'rnn_type': supported_rnns_inv.get(model.rnn_type, model.rnn_type.__name__.lower()),
            'labels': model._labels,
            'state_dict': model.state_dict()
       }
        if optimizer is not None:
            package['optim_dict'] = optimizer.state_dict()
        if avg_loss is not None:
            package['avg_loss'] = avg_loss
        if epoch is not None:
            package['epoch'] = epoch + 1  # increment for readability
        if iteration is not None:
            package['iteration'] = iteration
        if loss_results is not None:
            package['loss_results'] = loss_results
            package['cer_results'] = cer_results
            package['wer_results'] = wer_results
        if meta is not None:
            package['meta'] = meta
        return package

    @staticmethod
    def get_labels(model):
        """
        model_is_cuda = next(model.parameters()).is_cuda
        return model.module._labels if model_is_cuda else model._labels
        """
        return model._labels

    @staticmethod
    def get_param_size(model):
        params = 0
        for p in model.parameters():
            tmp = 1
            for x in p.size():
                tmp *= x
            params += tmp
        return params


    @staticmethod
    def get_audio_conf(model):
        return model._audio_conf


    @staticmethod
    def get_meta(model):
        model_is_cuda = next(model.parameters()).is_cuda
        m = model.module if model_is_cuda else model
        meta = {
            "version": m._version,
            "rnn_size": m.rnn_size,
            "rnn_layers": m.rnn_layers,
            "cnn_map": m.cnn_map,
            "cnn_kernel": m.cnn_kernel,
            "cnn_stride": m.cnn_stride,
            "cnn_layers": m.cnn_layers,
            "rnn_type": supported_rnns_inv[m.rnn_type]
        }
        return meta


if __name__ == '__main__':
    import os.path
    import argparse

    parser = argparse.ArgumentParser(description='DeepSpeech model information')
    parser.add_argument('--model_path', default='models/deepspeech_final.pth.tar',
                        help='Path to model file created by training')
    args = parser.parse_args()
    package = torch.load(args.model_path, map_location=lambda storage, loc: storage)
    model = DeepSpeech.load_model(args.model_path)

    print("Model name:         ", os.path.basename(args.model_path))
    print("DeepSpeech version: ", model._version)
    print("")
    print("Recurrent Neural Network Properties")
    print("  RNN Type:         ", model._rnn_type.__name__.lower())
    print("  RNN Layers:       ", model._hidden_layers)
    print("  RNN Size:         ", model._hidden_size)
    print("  Classes:          ", len(model._labels))
    print("")
    print("Model Features")
    print("  Labels:           ", model._labels)
    print("  Sample Rate:      ", model._audio_conf.get("sample_rate", "n/a"))
    print("  Window Type:      ", model._audio_conf.get("window", "n/a"))
    print("  Window Size:      ", model._audio_conf.get("window_size", "n/a"))
    print("  Window Stride:    ", model._audio_conf.get("window_stride", "n/a"))

    if package.get('loss_results', None) is not None:
        print("")
        print("Training Information")
        epochs = package['epoch']
        print("  Epochs:           ", epochs)
        print("  Current Loss:      {0:.3f}".format(package['loss_results'][epochs - 1]))
        print("  Current CER:       {0:.3f}".format(package['cer_results'][epochs - 1]))
        print("  Current WER:       {0:.3f}".format(package['wer_results'][epochs - 1]))

    if package.get('meta', None) is not None:
        print("")
        print("Additional Metadata")
        for k, v in model._meta:
            print("  ", k, ": ", v)
