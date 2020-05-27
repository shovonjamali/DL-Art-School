import argparse
import argparse
import logging
import os.path as osp
import os
import subprocess
import time

import torch
import torch.utils.data as data
import torchvision.transforms.functional as F
from PIL import Image
from tqdm import tqdm

import options.options as option
import utils.util as util
from data import create_dataloader
from models import create_model
import glob


class FfmpegBackedVideoDataset(data.Dataset):
    '''Pulls frames from a video one at a time using FFMPEG.'''

    def __init__(self, opt, working_dir):
        super(FfmpegBackedVideoDataset, self).__init__()
        self.opt = opt
        self.video = self.opt['video_file']
        self.working_dir = working_dir
        self.frame_rate = self.opt['frame_rate']
        self.start_at = self.opt['start_at_seconds']
        self.end_at = self.opt['end_at_seconds']
        self.frame_count = (self.end_at - self.start_at) * self.frame_rate
        # The number of (original) video frames that will be stored on the filesystem at a time.
        self.max_working_files = 20

        self.data_type = self.opt['data_type']
        self.vertical_splits = self.opt['vertical_splits'] if 'vertical_splits' in opt.keys() else 1

    def get_time_for_it(self, it):
        secs = it / self.frame_rate + self.start_at
        mins = int(secs / 60)
        secs = secs - (mins * 60)
        return '%02d:%06.3f' % (mins, secs)

    def __getitem__(self, index):
        if self.vertical_splits > 0:
            actual_index = int(index / self.vertical_splits)
        else:
            actual_index = index

        # Extract the frame. Command template: `ffmpeg -ss 17:00.0323 -i <video file>.mp4 -vframes 1 destination.png`
        working_file_name = osp.join(self.working_dir, "working_%d.png" % (actual_index % self.max_working_files,))
        vid_time = self.get_time_for_it(actual_index)
        ffmpeg_args = ['ffmpeg', '-y', '-ss', vid_time, '-i', self.video, '-vframes', '1', working_file_name]
        process = subprocess.Popen(ffmpeg_args, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        process.wait()

        # get LQ image
        LQ_path = working_file_name
        img_LQ = Image.open(LQ_path)
        split_index = (index % self.vertical_splits)
        if self.vertical_splits > 0:
            w, h = img_LQ.size
            w_per_split = int(w / self.vertical_splits)
            left = w_per_split * split_index
            img_LQ = F.crop(img_LQ, 0, left, h, w_per_split)
        img_LQ = F.to_tensor(img_LQ)

        return {'LQ': img_LQ}

    def __len__(self):
        return self.frame_count * self.vertical_splits


if __name__ == "__main__":
    #### options
    torch.backends.cudnn.benchmark = True
    want_just_images = True
    parser = argparse.ArgumentParser()
    parser.add_argument('-opt', type=str, help='Path to options YMAL file.', default='../options/use_video_upsample.yml')
    opt = option.parse(parser.parse_args().opt, is_train=False)
    opt = option.dict_to_nonedict(opt)

    util.mkdirs(
        (path for key, path in opt['path'].items()
         if not key == 'experiments_root' and 'pretrain_model' not in key and 'resume' not in key))
    util.setup_logger('base', opt['path']['log'], 'test_' + opt['name'], level=logging.INFO,
                      screen=True, tofile=True)
    logger = logging.getLogger('base')
    logger.info(option.dict2str(opt))

    #### Create test dataset and dataloader
    test_loaders = []

    test_set = FfmpegBackedVideoDataset(opt['dataset'], opt['path']['results_root'])
    test_loader = create_dataloader(test_set, opt['dataset'])
    logger.info('Number of test images in [{:s}]: {:d}'.format(opt['dataset']['name'], len(test_set)))
    test_loaders.append(test_loader)

    model = create_model(opt)
    test_set_name = test_loader.dataset.opt['name']
    logger.info('\nTesting [{:s}]...'.format(test_set_name))
    test_start_time = time.time()
    dataset_dir = osp.join(opt['path']['results_root'], test_set_name)
    util.mkdir(dataset_dir)

    frame_counter = 0
    frames_per_vid = opt['frames_per_mini_vid']
    minivid_bitrate = opt['mini_vid_bitrate']
    vid_counter = 0

    tq = tqdm(test_loader)
    for data in tq:
        need_GT = False if test_loader.dataset.opt['dataroot_GT'] is None else True
        model.feed_data(data, need_GT=need_GT)
        model.test()

        if isinstance(model.fake_H, tuple):
            visuals = model.fake_H[0].detach().float().cpu()
        else:
            visuals = model.fake_H.detach().float().cpu()
        for i in range(visuals.shape[0]):
            sr_img = util.tensor2img(visuals[i])  # uint8

            # save images
            save_img_path = osp.join(dataset_dir, '%08d.png' % (frame_counter,))
            util.save_img(sr_img, save_img_path)
            frame_counter += 1


            if frame_counter % frames_per_vid == 0:
                print("Encoding minivid %d.." % (vid_counter,))
                # Perform stitching.
                num_splits = opt['dataset']['vertical_splits'] if 'vertical_splits' in opt['dataset'].keys() else 1
                if num_splits > 1:
                    imgs = glob.glob(osp.join(dataset_dir, "*.png"))
                    procs = []
                    src_imgs_path = osp.join(dataset_dir, "joined")
                    os.makedirs(src_imgs_path, exist_ok=True)
                    for i in range(int(frames_per_vid / num_splits)):
                        to_join = [imgs[j] for j in range(i * num_splits, i * num_splits + num_splits)]
                        cmd = ['magick', 'convert'] + to_join + ['+append', osp.join(src_imgs_path, "%08d.png" % (i,))]
                        procs.append(subprocess.Popen(cmd))
                    for p in procs:
                        p.wait()
                else:
                    src_imgs_path = dataset_dir

                # Encoding command line:
                # ffmpeg -r 29.97 -f image2 -start_number 0 -i %08d.png -i ../wha_audio.mp3 -vcodec mpeg4 -vb 80M -r 29.97 -q:v 0 test.avi
                cmd = ['ffmpeg', '-y', '-r', str(opt['dataset']['frame_rate']), '-f', 'image2', '-start_number', '0', '-i', osp.join(src_imgs_path, "%08d.png"),
                       '-vcodec', 'mpeg4', '-vb', minivid_bitrate, '-r', str(opt['dataset']['frame_rate']), '-q:v', '0', osp.join(dataset_dir, "mini_%06d.mp4" % (vid_counter,))]
                process = subprocess.Popen(cmd, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                process.wait()
                vid_counter += 1
                frame_counter = 0
                print("Done.")


            if want_just_images:
                continue