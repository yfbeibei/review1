import scipy.spatial
import torch
import torch.nn.functional as F
from torch.utils.data import Dataset
import os
from image import load_data
import random
from PIL import Image
import numpy as np


def gen_discrete_map(im_height, im_width, points):
    """
        func: generate the discrete map.
        points: [num_gt, 2], for each row: [width, height]
        """
    discrete_map = np.zeros([im_height, im_width], dtype=np.float32)
    h, w = discrete_map.shape[:2]
    num_gt = points.shape[0]
    if num_gt == 0:
        return discrete_map
    
    # fast create discrete map
    points_np = np.array(points).round().astype(int)
    p_h = np.minimum(points_np[:, 1], np.array([h-1]*num_gt).astype(int))
    p_w = np.minimum(points_np[:, 0], np.array([w-1]*num_gt).astype(int))
    p_index = torch.from_numpy(p_h* im_width + p_w)
    discrete_map = torch.zeros(im_width * im_height).scatter_add_(0, index=p_index, src=torch.ones(im_width*im_height)).view(im_height, im_width).numpy()

    ''' slow method
    for p in points:
        p = np.round(p).astype(int)
        p[0], p[1] = min(h - 1, p[1]), min(w - 1, p[0])
        discrete_map[p[0], p[1]] += 1
    '''
    assert np.sum(discrete_map) == num_gt
    return discrete_map

def gaussian_filter_density(gt):
    print(gt.shape)
    density = np.zeros(gt.shape, dtype=np.float32)
    gt_count = np.count_nonzero(gt)
    if gt_count == 0:
        return density

    pts = np.array(zip(np.nonzero(gt)[1], np.nonzero(gt)[0]))
    leafsize = 2048
    # build kdtree
    tree = scipy.spatial.KDTree(pts.copy(), leafsize=leafsize)
    # query kdtree
    distances, locations = tree.query(pts, k=4)

    print('generate density...')
    for i, pt in enumerate(pts):
        pt2d = np.zeros(gt.shape, dtype=np.float32)
        pt2d[pt[1],pt[0]] = 1.
        if gt_count > 1:
            sigma = (distances[i][1]+distances[i][2]+distances[i][3])*0.1
        else:
            sigma = np.average(np.array(gt.shape))/2./2. #case: 1 point
        density += scipy.ndimage.filters.gaussian_filter(pt2d, sigma, mode='constant')
    print('done.')
    return density

class listDataset(Dataset):
    def __init__(self, root, shape=None, shuffle=True, transform=None, train=False, seen=0, batch_size=1,
                 num_workers=4, args=None):
        if train:
            random.shuffle(root)

        self.nSamples = len(root)
        self.lines = root
        self.transform = transform
        self.train = train
        self.shape = shape
        self.seen = seen
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.args = args

        self.rate = 1
        self.count = 1
        self.old_rate = []

    def __len__(self):
        return self.nSamples

    def __getitem__(self, index):
        assert index <= len(self), 'index range error'

        img_path = self.lines[index]
        fname = os.path.basename(img_path)
        img, kpoint = load_data(img_path, self.args, self.train)

        while min(kpoint.shape[0], kpoint.shape[1]) < self.args['crop_size']  and self.train == True:
            img_path = self.lines[random.randint(1, self.nSamples-1)]
            fname = os.path.basename(img_path)
            img, kpoint = load_data(img_path, self.args, self.train)

        '''data augmention'''
        if self.train == True:
            if random.random() > 0.5:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
                kpoint = np.fliplr(kpoint)

            if self.args['scale_aug'] == True and random.random() > (1 - self.args['scale_p']): # random scale
                if self.args['scale_type'] == 0:
                    self.rate = random.choice([0.8, 0.9, 1.1, 1.2])
                elif self.args['scale_type'] == 1:
                    self.rate = random.choice([0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3])
                elif self.args['scale_type'] == 2:
                    self.rate = random.uniform(0.7, 1.3)
                width, height = img.size
                width = int(width * self.rate)
                height = int(height * self.rate)
                if min(width, height) > self.args['crop_size']:
                    img = img.resize((width, height), Image.LANCZOS)
                else:
                    self.rate = 1
            else:
                self.rate = random.uniform(1.0, 1.0)

        kpoint = kpoint.copy()
        img = img.copy()

        if self.transform is not None:
            img = self.transform(img)


        if self.train == True:
            count_none = 0
            imgs = []
            targets = []
            for l in range(self.args['num_patch']):
                while True:
                    target = {}
                    if count_none > 100:
                        img_path = self.lines[random.randint(1, self.nSamples-1)]
                        fname = os.path.basename(img_path)
                        img, kpoint = load_data(img_path, self.args, self.train)

                        count_none = 0
                        if self.transform is not None:
                            img = self.transform(img)
                        self.rate = 1

                    width = self.args['crop_size']
                    height = self.args['crop_size']
                    try:
                        crop_size_x = random.randint(0, img.shape[1] - width)
                        crop_size_y = random.randint(0, img.shape[2] - height)
                    except:
                        count_none = 1000
                        continue

                    '''crop image'''
                    sub_img = img[:, crop_size_x: crop_size_x + width, crop_size_y:crop_size_y + height]
                    '''crop kpoint'''
                    crop_size_x = int(crop_size_x / self.rate)
                    crop_size_y = int(crop_size_y / self.rate)
                    width = int(width / self.rate)
                    height = int(height / self.rate)
                    sub_kpoint = kpoint[crop_size_x: crop_size_x + width, crop_size_y:crop_size_y + height]
                    sub_kpoint[sub_kpoint != 1] = 0
                    '''num_points and points'''
                    num_points = int(np.sum(sub_kpoint))
                    '''points'''
                    gt_points = np.nonzero(torch.from_numpy(sub_kpoint))

                    distances = self.caculate_knn_distance(gt_points, num_points)
                    points = torch.cat([gt_points, distances], dim=1)

                    #points = gt_points
                    # added by gls
                    gt_points_scale=gt_points/float(width)*self.args['crop_size']
                    # print(gt_points_scale.shape, gt_points_scale.dtype);print(gt_points_scale[0,:]);exit()
                    assert gt_points_scale.dim()==2
                    gt_points_scale=torch.flip(gt_points_scale, [1])
                    discrete_map=gen_discrete_map(self.args['crop_size'], self.args['crop_size'],gt_points_scale)


                    if num_points > self.args['min_num'] and num_points < self.args['num_queries']:
                        break

                    count_none += 1

                target['labels'] = torch.ones([1, num_points]).squeeze(0).type(torch.LongTensor)
                target['points_macher'] = torch.true_divide(points, width).type(torch.FloatTensor)
                target['points'] = torch.true_divide(points[:, 0:self.args['channel_point']], width).type(torch.FloatTensor)
                # added by gls
                target['gt_map'] = torch.from_numpy(discrete_map).unsqueeze(0)
                target['points_true'] = gt_points_scale.type(torch.FloatTensor)
                # target['points_true'] = torch.flip(gt_points_scale, [1]).type(torch.FloatTensor)
                # print("sub_kpoint shape: ", sub_kpoint.shape, sub_img.shape, img.shape, kpoint.shape)

                imgs.append(sub_img)
                targets.append(target)

            return fname, imgs, targets

        else:

            kpoint = torch.from_numpy(kpoint).cuda()

            padding_h = img.shape[1] % self.args['crop_size']
            padding_w = img.shape[2] % self.args['crop_size']

            if padding_w != 0:
                padding_w = self.args['crop_size'] - padding_w
            if padding_h != 0:
                padding_h = self.args['crop_size'] - padding_h

            '''for padding'''
            pd = (padding_w, 0, padding_h, 0)
            img = F.pad(img, pd, 'constant')
            kpoint = F.pad(kpoint, pd, 'constant').unsqueeze(0)

            width, height = img.shape[2], img.shape[1]
            num_w = int(width / self.args['crop_size'])
            num_h = int(height / self.args['crop_size'])

            '''image to patch'''
            img_return = img.view(3, num_h, self.args['crop_size'], width).view(3, num_h, self.args['crop_size'], num_w,
                                                                                self.args['crop_size'])
            img_return = img_return.permute(0, 1, 3, 2, 4).contiguous().view(3, num_w * num_h, self.args['crop_size'],
                                                                             self.args['crop_size']).permute(1, 0, 2, 3)

            kpoint_return = kpoint.view(num_h, self.args['crop_size'], width).view(num_h, self.args['crop_size'], num_w,
                                                                                   self.args['crop_size'])
            kpoint_return = kpoint_return.permute(0, 2, 1, 3).contiguous().view(num_w * num_h, 1, self.args['crop_size'],
                                                                                self.args['crop_size'])

            targets = []
            patch_info = [num_h, num_w, height, width, self.args['crop_size'], padding_w, padding_h]
            return fname, img_return, kpoint_return, targets, patch_info


    def caculate_knn_distance(self, gt_points, num_point):

        if num_point >= 4:
            tree = scipy.spatial.cKDTree(gt_points, leafsize=2048)
            distances, locations = tree.query(gt_points, k=min(self.args['num_knn'], num_point))
            distances = np.delete(distances, 0, axis=1)
            distances = np.mean(distances, axis=1)
            distances = torch.from_numpy(distances).unsqueeze(1)

        elif num_point == 0:
            distances = gt_points.clone()[:, 0].unsqueeze(1)

        elif num_point == 1:
            tree = scipy.spatial.cKDTree(gt_points, leafsize=2048)
            distances, locations = tree.query(gt_points, k=num_point)
            distances = torch.from_numpy(distances).unsqueeze(1)

        elif num_point == 2:
            tree = scipy.spatial.cKDTree(gt_points, leafsize=2048)
            distances, locations = tree.query(gt_points, k=num_point)
            distances = np.delete(distances, 0, axis=1)
            distances = (distances[:, 0]) / 1.0
            distances = torch.from_numpy(distances).unsqueeze(1)

        elif num_point == 3:
            tree = scipy.spatial.cKDTree(gt_points, leafsize=2048)
            distances, locations = tree.query(gt_points, k=num_point)
            distances = np.delete(distances, 0, axis=1)
            distances = (distances[:, 0] + distances[:, 1]) / 2
            distances = torch.from_numpy(distances).unsqueeze(1)

        return distances


