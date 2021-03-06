import os
import argparse
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
import numpy as np
from functools import partial
from collections import OrderedDict
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.nn.functional as F
from pytorch_bcnn.datasets import ImageDataset
from pytorch_bcnn.data.augmentor import DataAugmentor, Flip2D, Affine2D, ResizeCrop2D
from pytorch_bcnn.data.normalizer import Normalizer, Clip2D, Subtract2D, Divide2D
from pytorch_bcnn.models import BayesianUNet
from pytorch_bcnn.links import Regressor
from pytorch_bcnn.links import MCSampler
from pytorch_bcnn.models import PatchDiscriminator
from pytorch_bcnn.updaters import DCGANUpdater, LSGANUpdater
from pytorch_bcnn.inference import Inferencer
from pytorch_bcnn.visualizer import ImageVisualizer
from pytorch_bcnn.data import load_image, save_image
from pytorch_bcnn.datasets import train_valid_split
from pytorch_bcnn.extensions import LogReport
from pytorch_bcnn.extensions import PrintReport
from pytorch_bcnn.extensions import Validator
from pytorch_bcnn.utils import save_args
from pytorch_bcnn.utils import fixed_seed
from pytorch_bcnn.utils import find_latest_snapshot
from pytorch_trainer import iterators
from pytorch_trainer import dataset
from pytorch_trainer import training
from pytorch_trainer.training import extensions
from pytorch_trainer.training import triggers

def build_discriminator():

    conv_param = {
        'name':'conv',
        'kernel_size': 4,
        'stride': 2,
        'padding': 2,
        'padding_mode': 'zeros',
        'initialW': {'name': 'normal', 'std': 0.02},
        'initial_bias': {'name': 'zero'},
        'hook': {'name': 'spectral_normalization'}
    }

    pool_param = {
        'name': 'none',
    }

    norm_param = None

    activation_param = {
        'name': 'leaky_relu'
    }

    dropout_param = {
        'name': 'none'
    }

    discriminator = PatchDiscriminator(
                        ndim=2,
                        in_channels=3*2,
                        out_channels=1,
                        nlayer=4,
                        nfilter=64,
                        ninner=1,
                        conv_param=conv_param,
                        pool_param=pool_param,
                        norm_param=norm_param,
                        activation_param=activation_param,
                        dropout_param=dropout_param,
                        preserve_color=True)

    return discriminator


def build_generator():

    conv_param = {
        'name':'conv',
        'kernel_size': 4,
        'stride': 1,
        'padding': 2,
        'padding_mode': 'reflect',
        'initialW': {'name': 'normal', 'std': 0.02},
        'bias': False,
    }

    pool_param = {
        'name': 'stride',
        'stride': 2,
    }

    upconv_param = {
        'name':'deconv',
        'kernel_size': 4,
        'stride': 2,
        'padding': 0,
        'padding_mode': 'zeros',
        'initialW': {'name': 'normal', 'std': 0.02},
        'bias': False,
    }

    norm_param = {
        'name': 'batch'
    }

    activation_param = {
        'name': 'leaky_relu'
    }

    exp_activation_param = {
        'name': 'relu'
    }

    dropout_param = {
        'name': 'none',
    }

    exp_dropout_param = {
        'name': 'mc_dropout',
    }

    generator = BayesianUNet(
                    ndim=2,
                    in_channels=3,
                    out_channels=3,
                    nlayer=8,
                    nfilter=[64,128,256,512,512,512,512,512],
                    ninner=1,
                    conv_param=conv_param,
                    pool_param=pool_param,
                    upconv_param=upconv_param,
                    norm_param=norm_param,
                    activation_param=activation_param,
                    dropout_param=dropout_param,
                    dropout_enables=[False,False,False,False,True,True,True,False],
                    preserve_color=True,
                    exp_ninner=0,
                    exp_activation_param=exp_activation_param,
                    exp_dropout_param=exp_dropout_param)

    return generator


def eval_metric(y, t):
    def mean_absolute_error(y, t):
        y = y.astype(np.float32)
        t = t.astype(np.float32)
        return np.mean(np.abs(y-t))
    return mean_absolute_error(y, t)


def train_phase(generator, train, valid, args):

    print('# samples:')
    print('-- train:', len(train))
    print('-- valid:', len(valid))

    # setup dataset iterators
    train_iter = iterators.SerialIterator(train, args.batchsize)
    valid_iter = iterators.SerialIterator(valid, args.batchsize,
                                                repeat=False, shuffle=True)

    # setup a model
    model = Regressor(generator,
                      activation=torch.tanh,
                      lossfun=F.l1_loss,
                      accfun=F.l1_loss)

    discriminator = build_discriminator()
    discriminator.save_args(os.path.join(args.out, 'discriminator.json'))

    device = torch.device(args.gpu)

    model.to(device)
    discriminator.to(device)

    # setup an optimizer
    optimizer_G = torch.optim.Adam(model.parameters(),
                                   lr=args.lr,
                                   betas=(args.beta, 0.999),
                                   weight_decay=max(args.decay, 0))

    optimizer_D = torch.optim.Adam(discriminator.parameters(),
                                   lr=args.lr,
                                   betas=(args.beta, 0.999),
                                   weight_decay=max(args.decay, 0))

    # setup a trainer
    updater = DCGANUpdater(
        iterator=train_iter,
        optimizer={
            'gen': optimizer_G,
            'dis': optimizer_D,
        },
        model={
            'gen': model,
            'dis': discriminator,
        },
        alpha=args.alpha,
        device=args.gpu,
    )

    frequency = max(args.iteration//80, 1) if args.frequency == -1 else max(1, args.frequency)

    stop_trigger = triggers.EarlyStoppingTrigger(monitor='validation/main/loss',
                        max_trigger=(args.iteration, 'iteration'),
                        check_trigger=(frequency, 'iteration'),
                        patients=np.inf if args.pinfall == -1 else max(1, args.pinfall))

    trainer = training.Trainer(updater, stop_trigger, out=args.out)

    # shift lr
    trainer.extend(
        extensions.LinearShift('lr', (args.lr, 0.0),
                        (args.iteration//2, args.iteration),
                        optimizer=optimizer_G))
    trainer.extend(
        extensions.LinearShift('lr', (args.lr, 0.0),
                        (args.iteration//2, args.iteration),
                        optimizer=optimizer_D))

    # setup a visualizer

    transforms = {'x': lambda x: x, 'y': lambda x: x, 't': lambda x: x}
    clims = {'x': (-1., 1.), 'y': (-1., 1.), 't': (-1., 1.)}

    visualizer = ImageVisualizer(transforms=transforms,
                                 cmaps=None,
                                 clims=clims)

    # setup a validator
    valid_file = os.path.join('validation', 'iter_{.updater.iteration:08}.png')
    trainer.extend(Validator(valid_iter, model, valid_file,
                             visualizer=visualizer, n_vis=20,
                             device=args.gpu),
                             trigger=(frequency, 'iteration'))

    # trainer.extend(DumpGraph('loss_gen', filename='generative_loss.dot'))
    # trainer.extend(DumpGraph('loss_cond', filename='conditional_loss.dot'))
    # trainer.extend(DumpGraph('loss_dis', filename='discriminative_loss.dot'))

    trainer.extend(extensions.snapshot(filename='snapshot_iter_{.updater.iteration:08}.pth'),
                                       trigger=(frequency, 'iteration'))
    trainer.extend(extensions.snapshot_object(generator, 'generator_iter_{.updater.iteration:08}.pth'),
                                              trigger=(frequency, 'iteration'))
    trainer.extend(extensions.snapshot_object(discriminator, 'discriminator_iter_{.updater.iteration:08}.pth'),
                                              trigger=(frequency, 'iteration'))

    log_keys = ['loss_gen', 'loss_cond', 'loss_dis',
                'validation/main/accuracy']

    trainer.extend(LogReport(keys=log_keys, trigger=(100, 'iteration')))

    # setup log ploter
    if extensions.PlotReport.available():
        for plot_key in ['loss', 'accuracy']:
            plot_keys = [key for key in log_keys if key.split('/')[-1].startswith(plot_key)]
            trainer.extend(
                extensions.PlotReport(plot_keys,
                                     'iteration', file_name=plot_key + '.png',
                                     trigger=(frequency, 'iteration')) )

    trainer.extend(PrintReport(['iteration'] + log_keys + ['elapsed_time'], n_step=1))

    trainer.extend(extensions.ProgressBar())

    if args.resume:
        trainer.load_state_dict(torch.load(args.resume))


    # train
    trainer.run()


def test_phase(generator, test, args):

    print('# samples:')
    print('-- test:', len(test))

    test_iter = iterators.SerialIterator(test, args.batchsize, repeat=False, shuffle=False)

    # setup a inferencer
    snapshot_file = find_latest_snapshot('generator_iter_{.updater.iteration:08}.pth', args.out)
    generator.load_state_dict(torch.load(snapshot_file))
    print('Loaded a snapshot:', snapshot_file)

    model = MCSampler(generator,
                      mc_iteration=args.mc_iteration,
                      activation=torch.tanh,
                      reduce_mean=None,
                      reduce_var=partial(torch.mean, dim=1))

    device = torch.device(args.gpu)
    model.to(device)

    infer = Inferencer(test_iter, model, device=args.gpu)

    pred, uncert = infer.run()


    # evaluate
    os.makedirs(os.path.join(args.out, 'test'), exist_ok=True)

    acc_values = []
    uncert_values = []

    uncert_clim = (0, np.percentile(uncert, 95))
    error_clim = (0, 1)

    files = test.files['image']
    if isinstance(files, np.ndarray): files = files.tolist()
    commonpath = os.path.commonpath(files)

    plt.rcParams['font.size'] = 14

    for i, (p, u, imf, lbf) in enumerate(zip(pred, uncert,
                                             test.files['image'],
                                             test.files['label'])):
        im, _ = load_image(imf)
        lb, _ = load_image(lbf)
        im = im.astype(np.float32)
        lb = lb.astype(np.float32)

        p = p.transpose(1,2,0)

        im = (im[:,:,::-1] + 1.) / 2.
        lb = (lb[:,:,::-1] + 1.) / 2.
        p  = (p[:,:,::-1] + 1.) / 2.

        error = np.mean(np.abs(p-lb), axis=-1)

        acc_values.append( eval_metric(p,lb) )
        uncert_values.append( np.mean(u) )


        plt.figure(figsize=(20,4))

        for j, (pic, cmap, clim, title) in enumerate(zip(
                                        [im, p, lb, u, error],
                                        [None, None, None, 'jet', 'jet'],
                                        [None, None, None, uncert_clim, error_clim],
                                        ['Input image\n%s' % os.path.relpath(imf, commonpath),
                                             'Predicted label\n(MAE=%.3f)' % acc_values[-1],
                                             'Ground-truth label',
                                             'Predicted variance\n(PV=%.4f)' % uncert_values[-1],
                                             'Error'])):
            plt.subplot(1,5, j+1)
            plt.imshow(pic, cmap=cmap)
            plt.xticks([], [])
            plt.yticks([], [])
            plt.title(title)
            plt.clim(clim)

        plt.tight_layout()
        plt.savefig(os.path.join(args.out, 'test/%03d.png' % i))
        plt.close()


def get_dataset(data_root,
                valid_split_ratio,
                valid_augment,
                normalizer=None,
                augmentor=None):

    class_list = None
    dtypes = OrderedDict({'image': np.float32, 'label': np.float32})

    getter = partial(ImageDataset, root=data_root, classes=class_list,
                        dtypes=dtypes, normalizer=normalizer)

    # train and valid dataset
    train_patients = ['*']

    train_filenames = OrderedDict({
        'image': '{root}/train/{patient}_a.mha',
        'label': '{root}/train/{patient}_b.mha',
    })

    dataset = getter(patients=train_patients, filenames=train_filenames, augmentor=augmentor)
    train, valid = train_valid_split(dataset, valid_split_ratio)

    if not valid_augment:
        del valid.augmentor

    # test dataset
    test_patients = ['*']

    test_filenames = OrderedDict({
        'image': '{root}/val/{patient}_a.mha',
        'label': '{root}/val/{patient}_b.mha',
    })

    test = getter(patients=test_patients, filenames=test_filenames, augmentor=None)

    return train, valid, test


def get_normalizer():

    normalizer = Normalizer()
    normalizer.add(Subtract2D(0.))

    return normalizer


def get_augmentor():

    augmentor = DataAugmentor()
    augmentor.add(ResizeCrop2D(resize_size=(286,286),
                               crop_size=(256,256)))
    augmentor.add(Flip2D(1))
    augmentor.add(Flip2D(2))

    return augmentor


def main():

    parser = argparse.ArgumentParser(description='Example: Uncertainty estimates with adversarial training in image synthesis',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--data_root', '-d', type=str, default='./preprocessed',
                        help='Directory to dataset')
    parser.add_argument('--batchsize', '-b', type=int, default=5,
                        help='Number of images in each mini-batch')
    parser.add_argument('--iteration', '-i', type=int, default=200000,
                        help='Number of sweeps over the dataset to train')
    parser.add_argument('--frequency', '-f', type=int, default=-1,
                        help='Frequency of taking a snapshot')
    parser.add_argument('--gpu', '-g', type=str, default='cuda:0',
                        help='GPU Device')
    parser.add_argument('--out', '-o', default='logs',
                        help='Directory to output the result')
    parser.add_argument('--resume', '-r', default='',
                        help='Resume the training from snapshot')
    parser.add_argument('--valid_augment', action='store_true',
                        help='Enable data augmentation during validation')
    parser.add_argument('--valid_split_ratio', type=float, default=0.1,
                        help='Ratio of validation data to training data')
    parser.add_argument('--lr', type=float, default=4e-4,
                        help='Learning rate')
    parser.add_argument('--alpha', type=float, default=50.,
                        help='Weight of conditional loss')
    parser.add_argument('--beta', type=float, default=0.5,
                        help='Exponential decay rate of the first order moment in Adam')
    parser.add_argument('--decay', type=float, default=-1,
                        help='Weight of L2 regularization')
    parser.add_argument('--mc_iteration', type=int, default=15,
                        help='Number of iteration of MCMC')
    parser.add_argument('--pinfall', type=int, default=-1,
                        help='Countdown for early stopping of training.')
    parser.add_argument('--freeze_upconv', action='store_true',
                        help='Disables updating the up-convolutional weights. If weights are initialized with \
                            bilinear kernels, up-conv acts as bilinear upsampler.')
    parser.add_argument('--test_on_test', action='store_true',
                        help='Switch to the testing phase on test dataset')
    parser.add_argument('--test_on_valid', action='store_true',
                        help='Switch to the testing phase on valid dataset')
    parser.add_argument('--seed', type=int, default=0,
                        help='Fix the random seed')
    args = parser.parse_args()

    print('GPU: {}'.format(args.gpu))
    print('# Minibatch-size: {}'.format(args.batchsize))
    print('')

    # setup output directory
    os.makedirs(args.out, exist_ok=True)

    # NOTE: ad-hoc
    normalizer = get_normalizer()
    augmentor = get_augmentor()

    # setup a generator
    with fixed_seed(args.seed, strict=False):

        generator = build_generator()

        if args.freeze_upconv:
            generator.freeze_layers(name='upconv',
                                    recursive=True,
                                    verbose=True)

        # setup dataset
        train, valid, test = get_dataset(args.data_root,
                                         args.valid_split_ratio,
                                         args.valid_augment,
                                         normalizer, augmentor)

        # run
        if args.test_on_test:
            raise RuntimeError('This example is under construction. Please tune the hyperparameters first..')
            test_phase(generator, test, args)
        elif args.test_on_valid:
            test_phase(generator, valid, args)
        else:
            save_args(args, args.out)
            generator.save_args(os.path.join(args.out, 'model.json'))
            normalizer.summary(os.path.join(args.out, 'norm.json'))
            augmentor.summary(os.path.join(args.out, 'augment.json'))

            train_phase(generator, train, valid, args)


if __name__ == '__main__':
    main()
