from __future__ import print_function
import argparse
import os
import shutil
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
from triplet_cub_loader import CUB_t
from kNN_cub_loader import CUB_t_kNN
from tripletnet import Tripletnet
from visdom import Visdom
import numpy as np
from random import shuffle

import hard_mining

# Training settings
parser = argparse.ArgumentParser(description='PyTorch MNIST Example')
parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                    help='input batch size for training (default: 64)')
parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                    help='input batch size for testing (default: 1000)')
parser.add_argument('--kNN-train-size', type=int, default=1000, metavar='N',
                    help='input batch size for testing (default: 1000)')
parser.add_argument('--kNN-test-size', type=int, default=100, metavar='N',
                    help='input batch size for testing (default: 1000)')
parser.add_argument('--kNN-k', type=int, default=1, metavar='N',
                    help='k nearest neightbor (default: 1)')
parser.add_argument('--epochs', type=int, default=1, metavar='N',
                    help='number of epochs to train (default: 5)')
parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                    help='learning rate (default: 0.01)')
parser.add_argument('--momentum', type=float, default=0.5, metavar='M',
                    help='SGD momentum (default: 0.5)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')
parser.add_argument('--log-interval', type=int, default=20, metavar='N',
                    help='how many batches to wait before logging training status')
parser.add_argument('--margin', type=float, default=0.2, metavar='M',
                    help='margin for triplet loss (default: 0.2)')
parser.add_argument('--resume', default='', type=str,
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--name', default='TripletNet', type=str,
                    help='name of experiment')
parser.add_argument('--data', default='datasets/birds', type=str,
                    help='name of experiment')
parser.add_argument('--triplet_freq', type=int, default=3, metavar='N',
                    help='epochs before new triplets list (default: 3)')

best_acc = 0

hard_frac = 0.1
OurSampler = hard_mining.NHardestTripletSampler

def main():
    global args, best_acc
    args = parser.parse_args()
    data_path = args.data
    args.cuda = not args.no_cuda and torch.cuda.is_available()
    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)
    global plotter 
    plotter = VisdomLinePlotter(env_name=args.name)

    kwargs = {'num_workers': 1, 'pin_memory': True} if args.cuda else {}

    num_classes = 5
    num_triplets = num_classes*64
    train_data_set = CUB_t(data_path, n_train_triplets=num_triplets, train=True,
                           transform=transforms.Compose([
                             transforms.ToTensor(),
                             transforms.Normalize((0.1307,), (0.3081,))
                           ]),
                           num_classes=num_classes)
    train_loader = torch.utils.data.DataLoader(
        train_data_set,
        batch_size=args.batch_size, shuffle=True, **kwargs)
    test_loader = torch.utils.data.DataLoader(
        CUB_t(data_path, n_test_triplets=num_classes*16, train=False,
                 transform=transforms.Compose([
                           transforms.ToTensor(),
                           transforms.Normalize((0.1307,), (0.3081,))
                       ]),
             num_classes=num_classes),
        batch_size=args.test_batch_size, shuffle=True, **kwargs)
    
    kNN_loader = CUB_t_kNN(data_path, train=False,
                 n_test = args.kNN_test_size, n_train = args.kNN_train_size,
                 transform=transforms.Compose([
                           transforms.ToTensor(),
                           transforms.Normalize((0.1307,), (0.3081,))
                       ]))


    # image length 
    im_len = 64
    # size of first fully connected layer
    h1_len = (im_len-4)/2
    h2_len = (h1_len-4)/2
    fc1_len = h2_len*h2_len*20

    class Net(nn.Module):
        def __init__(self):
            super(Net, self).__init__()
            self.conv1 = nn.Conv2d(3, 10, kernel_size=5)
            self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
            self.conv2_drop = nn.Dropout2d()
            self.fc1 = nn.Linear(fc1_len, 50)
            self.fc2 = nn.Linear(50, 10)

        def forward(self, x):
            x = F.relu(F.max_pool2d(self.conv1(x), 2))
            x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
            x = x.view(-1, fc1_len)
            x = F.relu(self.fc1(x))
            x = F.dropout(x, training=self.training)
            return self.fc2(x)

    model = Net()
    tnet = Tripletnet(model)
    if args.cuda:
        tnet.cuda()

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            tnet.load_state_dict(checkpoint['state_dict'])
            print("=> loaded checkpoint '{}' (epoch {})"
                    .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    criterion = torch.nn.MarginRankingLoss(margin = args.margin)
    optimizer = optim.SGD(tnet.parameters(), lr=args.lr, momentum=args.momentum)

    n_parameters = sum([p.data.nelement() for p in tnet.parameters()])
    print('  + Number of params: {}'.format(n_parameters))

    sampler = OurSampler(num_classes, num_triplets/args.batch_size)
    
    for epoch in range(1, args.epochs + 1):
        # train for one epoch
        train(train_loader, tnet, criterion, optimizer, epoch, sampler)
        # evaluate on validation set
        acc = test(test_loader, tnet, criterion, epoch)
        acc_kNN = test_kNN(kNN_loader, tnet, epoch, args.kNN_k)

        # remember best acc and save checkpoint
        is_best = acc > best_acc
        best_acc = max(acc, best_acc)
        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': tnet.state_dict(),
            'best_prec1': best_acc,
        }, is_best)

        # reset sampler and regenerate triplets every few epochs
        if epoch % args.triplet_freq == 0:
            # TODO: regenerate triplets
            train_data_set.regenerate_triplet_list(num_triplets, sampler, num_triplets*hard_frac)
            # then reset sampler
            sampler.Reset()

def train(train_loader, tnet, criterion, optimizer, epoch, sampler):
    losses = AverageMeter()
    accs = AverageMeter()
    emb_norms = AverageMeter()
    
    # switch to train mode
    tnet.train()
    for batch_idx, (data1, data2, data3, idx1, idx2, idx3) in enumerate(train_loader):
        if args.cuda:
            data1, data2, data3 = data1.cuda(), data2.cuda(), data3.cuda()
        data1, data2, data3 = Variable(data1), Variable(data2), Variable(data3)

        # compute output
        dista, distb, embedded_x, embedded_y, embedded_z = tnet(data1, data2, data3)
        # 1 means, dista should be larger than distb
        target = torch.FloatTensor(dista.size()).fill_(1)
        if args.cuda:
            target = target.cuda()
        target = Variable(target)
        
        loss_triplet = criterion(dista, distb, target)
        sampler.SampleNegatives(dista, distb, loss_triplet, (idx1, idx2, idx3))
        
        loss_embedd = embedded_x.norm(2) + embedded_y.norm(2) + embedded_z.norm(2)
        loss = loss_triplet + 0.001 * loss_embedd

        # measure accuracy and record loss
        acc = accuracy(dista, distb)
        losses.update(loss_triplet.data[0], data1.size(0))
        accs.update(acc, data1.size(0))
        emb_norms.update(loss_embedd.data[0]/3, data1.size(0))

        # compute gradient and do optimizer step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if batch_idx % args.log_interval == 0:
            print('Train Epoch: {} [{}/{}]\t'
                  'Loss: {:.4f} ({:.4f}) \t'
                  'Acc: {:.2f}% ({:.2f}%) \t'
                  'Emb_Norm: {:.2f} ({:.2f})'.format(
                epoch, batch_idx * len(data1), len(train_loader.dataset),
                losses.val, losses.avg, 
                100. * accs.val, 100. * accs.avg, emb_norms.val, emb_norms.avg))
    # log avg values to somewhere
    plotter.plot('acc', 'train', epoch, accs.avg)
    plotter.plot('loss', 'train', epoch, losses.avg)
    plotter.plot('emb_norms', 'train', epoch, emb_norms.avg)

def test(test_loader, tnet, criterion, epoch):
    losses = AverageMeter()
    accs = AverageMeter()

    # switch to evaluation mode
    tnet.eval()
    for batch_idx, (data1, data2, data3, _, _, _) in enumerate(test_loader):
        if args.cuda:
            data1, data2, data3 = data1.cuda(), data2.cuda(), data3.cuda()
        data1, data2, data3 = Variable(data1), Variable(data2), Variable(data3)

        # compute output
        dista, distb, _, _, _ = tnet(data1, data2, data3)
        target = torch.FloatTensor(dista.size()).fill_(1)
        if args.cuda:
            target = target.cuda()
        target = Variable(target)
        test_loss =  criterion(dista, distb, target).data[0]

        # measure accuracy and record loss
        acc = accuracy(dista, distb)
        accs.update(acc, data1.size(0))
        losses.update(test_loss, data1.size(0))      

    print('\nTest set: Average loss: {:.4f}, Accuracy: {:.2f}%\n'.format(
        losses.avg, 100. * accs.avg))
    plotter.plot('acc', 'test', epoch, accs.avg)
    plotter.plot('loss', 'test', epoch, losses.avg)
    return accs.avg

def test_kNN(kNN_loader, tnet, epoch, k):    
    # switch to evaluation mode
    tnet.embeddingnet.eval()

    data_test, Y_test, data_train, Y_train = kNN_loader.getitem()
    Y_test = np.array(Y_test, dtype = np.int64)
    Y_train = np.array(Y_train, dtype = np.int64)
    
    # compute feature space
    if args.cuda:
        data_test = data_test.cuda()
        data_train = data_train.cuda()
    data_test = Variable(data_test)
    data_train = Variable(data_train)
    f_test = tnet.embeddingnet(data_test).data.numpy()
    f_train = tnet.embeddingnet(data_train).data.numpy()
    
    acc = kNN(f_test, f_train, Y_test, Y_train, k)
    
    print('\nkNN Test: Accuracy: {:.3f}%\n'.format(100 * acc))
    return acc
    
    
def kNN(f_test, f_train, Y_test, Y_train, k):
    num_test = f_test.shape[0]
    num_train = f_train.shape[0]
    Y_pred = np.zeros(num_test)
    
    # Compute distances
    dists = np.zeros((num_test, num_train)) 
    test_square = np.sum(np.square(f_test), axis=1).reshape(num_test, 1)
    train_square = np.sum(np.square(f_train), axis=1)
    multi = np.dot(f_test, np.transpose(f_train))
    dists = np.sqrt(test_square + train_square - 2 * multi)
    
    # k nearest label
    for i in xrange(num_test):
        closest_y = Y_train[np.argsort(dists[i, :])[:k]]
        Y_pred[i] = np.argmax(np.bincount(closest_y))
    Y_pred = np.array(Y_pred, dtype = np.int64)
    
    acc = sum([Y_pred[i]==Y_test[i] for i in range(num_test)]) / num_test
    
    return acc

def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    """Saves checkpoint to disk"""
    directory = "runs/%s/"%(args.name)
    if not os.path.exists(directory):
        os.makedirs(directory)
    filename = directory + filename
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'runs/%s/'%(args.name) + 'model_best.pth.tar')

class VisdomLinePlotter(object):
    """Plots to Visdom"""
    def __init__(self, env_name='main'):
        self.viz = Visdom()
        self.env = env_name
        self.plots = {}
    def plot(self, var_name, split_name, x, y):
        if var_name not in self.plots:
            self.plots[var_name] = self.viz.line(X=np.array([x,x]), Y=np.array([y,y]), env=self.env, opts=dict(
                legend=[split_name],
                title=var_name,
                xlabel='Epochs',
                ylabel=var_name
            ))
        else:
            self.viz.updateTrace(X=np.array([x]), Y=np.array([y]), env=self.env, win=self.plots[var_name], name=split_name)

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def accuracy(dista, distb):
    margin = 0
    pred = (dista - distb - margin).cpu().data
    return (pred > 0).sum()*1.0/dista.size()[0]

if __name__ == '__main__':
    main()  

