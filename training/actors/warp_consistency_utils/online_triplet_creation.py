import torch
import torch.nn.functional as F
from utils_flow.pixel_wise_mapping import warp
from utils_flow.flow_and_mapping_operations import create_border_mask
from datasets.util import define_mask_zero_borders


class BatchedImageTripletCreation:
    """ Class responsible for creating the image triplet used in the warp-consistency graph, from a pair of source and
    target images.
    Particularly, from the source and target image pair (which are usually not related by a known ground-truth flow),
    it creates an image triplet (source, target and target_prime) where the target prime image is related to the
    target by a known synthetically and randomly generated flow field (the flow generator is specified by user
    with argument 'synthetic_flow_generator').
    The final image triplet is obtained by cropping central patches of the desired dimensions in the three images.
    Optionally, two image triplets can be created with different synthetic flow fields, resulting in different
    target_prime images, to apply different losses on each.
    """

    def __init__(self, settings, synthetic_flow_generator, compute_mask_zero_borders=False,
                 min_percent_valid_corr=0.1, crop_size=256, output_size=256, padding_mode='zeros'):
        """
        Args:
            settings: settings
            synthetic_flow_generator: class responsible for generating a synthetic flow field.
            compute_mask_zero_borders: compute the mask of zero borders in target prime image? will be equal to 0
                                       where the target prime image is 0, 1 otherwise.
            min_percent_valid_corr: minimum percentage of matching regions between target prime and target. Otherwise,
                                    use ground-truth correspondence mask.
            crop_size: size of the center crop .
            output_size: size of the final outputted images and flow fields (resized after the crop).
        """
        self.compute_mask_zero_borders = compute_mask_zero_borders

        self.device = getattr(settings, 'device', None)
        if self.device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() and settings.use_gpu else "cpu")

        self.synthetic_flow_generator = synthetic_flow_generator
        if not isinstance(crop_size, tuple):
            crop_size = (crop_size, crop_size)
        self.crop_size = crop_size

        if not isinstance(output_size, tuple):
            output_size = (output_size, output_size)
        self.output_size = output_size
        self.min_percent_valid_corr = min_percent_valid_corr
        self.padding_mode = padding_mode

    def __call__(self, mini_batch, net=None, training=True,  *args, **kwargs):
        """
        Args:
            mini_batch: The mini batch input data, should at least contain the fields 'source_image', 'target_image'
            training: bool indicating if we are in training or evaluation mode
            net: network
        Returns:
            mini_batch: output data block with at least the fields 'source_image', 'target_image',
                        'target_image_prime', 'flow_map', 'correspondence_mask'.
                        If self.compute_mask_zero_borders, will contain field 'mask_zero_borders',
                        If ground-truth is known (was provided) between source and target image, will contain the fields
                        'flow_map_target_to_source', 'correspondence_mask_target_to_source'.
        """

        # take original images
        source_image = mini_batch['source_image'].to(self.device)
        target_image = mini_batch['target_image'].to(self.device)
        b, _, h, w = source_image.shape

        '''
        if h <= self.output_size[0] or w <= self.output_size[1]:
            # should be larger, otherwise the warping will create huge black borders
            print('Image or flow is the same size than desired output size in warping dataset ! ')
        '''

        # get synthetic homography transformation from the synthetic flow generator
        # flow_gt here is between target prime and target
        flow_gt = self.synthetic_flow_generator(mini_batch=mini_batch, training=training, net=net).detach()
        flow_gt.require_grad = False
        bs, _, h_f, w_f = flow_gt.shape
        if h_f != h or w_f != w:
            # reshape and rescale the flow so it has the load_size of the original images
            flow_gt = F.interpolate(flow_gt, (h, w), mode='bilinear', align_corners=False)
            flow_gt[:, 0] *= float(w) / float(w_f)
            flow_gt[:, 1] *= float(h) / float(h_f)
        target_image_prime = warp(target_image, flow_gt, padding_mode=self.padding_mode).byte()

        # if there exists a ground-truth flow between the source and target image, also modify it so it corresponds
        # to the new source and target images.
        if 'flow_map' in list(mini_batch.keys()):
            if isinstance(mini_batch['flow_map'], list):
                flow_gt_target_to_source = mini_batch['flow_map'][-1].to(self.device)
                mask_gt_target_to_source = mini_batch['correspondence_mask'][-1].to(self.device)
            else:
                flow_gt_target_to_source = mini_batch['flow_map'].to(self.device)
                mask_gt_target_to_source = mini_batch['correspondence_mask'].to(self.device)
        else:
            flow_gt_target_to_source = None
            mask_gt_target_to_source = None

        # crop a center patch from the images and the ground-truth flow field, so that black borders are removed
        x_start = w // 2 - self.crop_size[1] // 2
        y_start = h // 2 - self.crop_size[0] // 2
        source_image_resized = source_image[:, :, y_start: y_start + self.crop_size[0],
                               x_start: x_start + self.crop_size[1]]
        target_image_resized = target_image[:, :, y_start: y_start + self.crop_size[0],
                               x_start: x_start + self.crop_size[1]]
        target_image_prime_resized = target_image_prime[:, :, y_start: y_start + self.crop_size[0],
                                                        x_start: x_start + self.crop_size[1]]
        flow_gt_resized = flow_gt[:, :, y_start: y_start + self.crop_size[0], x_start: x_start + self.crop_size[1]]

        if 'flow_map' in list(mini_batch.keys()):
            flow_gt_target_to_source = flow_gt_target_to_source[:, :, y_start: y_start + self.crop_size[0],
                                                                x_start: x_start + self.crop_size[1]]
            if mask_gt_target_to_source is not None:
                mask_gt_target_to_source = mask_gt_target_to_source[:, y_start: y_start + self.crop_size[0],
                                                                    x_start: x_start + self.crop_size[1]]

        if 'target_kps' in mini_batch.keys():
            target_kp = mini_batch['target_kps'].to(self.device).clone()  # b, N, 2
            source_kp = mini_batch['source_kps'].to(self.device).clone()  # b, N, 2
            source_kp[:, :, 0] = source_kp[:, :, 0] - x_start   # will just make the not valid part even smaller
            source_kp[:, :, 1] = source_kp[:, :, 1] - y_start
            target_kp[:, :, 0] = target_kp[:, :, 0] - x_start
            target_kp[:, :, 1] = target_kp[:, :, 1] - y_start

        # resize to final outptu load_size, this is to prevent the crop from removing all common areas
        if self.output_size != self.crop_size:
            source_image_resized = F.interpolate(source_image_resized, self.output_size,
                                                 mode='area')
            target_image_resized = F.interpolate(target_image_resized, self.output_size,
                                                 mode='area')
            target_image_prime_resized = F.interpolate(target_image_prime_resized, self.output_size,
                                                       mode='area')
            flow_gt_resized = F.interpolate(flow_gt_resized, self.output_size,
                                            mode='bilinear', align_corners=False)
            flow_gt_resized[:, 0] *= float(self.output_size[1]) / float(self.crop_size[1])
            flow_gt_resized[:, 1] *= float(self.output_size[0]) / float(self.crop_size[0])

            if 'flow_map' in list(mini_batch.keys()):
                flow_gt_target_to_source = F.interpolate(flow_gt_target_to_source, self.output_size,
                                            mode='bilinear', align_corners=False)
                flow_gt_target_to_source[:, 0] *= float(self.output_size[1]) / float(self.crop_size[1])
                flow_gt_target_to_source[:, 1] *= float(self.output_size[0]) / float(self.crop_size[0])
                if mask_gt_target_to_source is not None:
                    mask_gt_target_to_source = F.interpolate(mask_gt_target_to_source.unsqueeze(1).float(),
                                                             self.output_size,
                                                             mode='bilinear', align_corners=False)
                    mask_gt_target_to_source = mask_gt_target_to_source.bool() if float(torch.__version__[:3]) >= 1.1 \
                        else mask_gt_target_to_source.byte()

            # if target kps, also resize them
            if 'target_kps' in mini_batch.keys():
                source_kp[:, :, 0] *= float(self.output_size[1]) / float(self.crop_size[1])
                source_kp[:, :, 1] *= float(self.output_size[0]) / float(self.crop_size[0])
                target_kp[:, :, 0] *= float(self.output_size[1]) / float(self.crop_size[1])
                target_kp[:, :, 1] *= float(self.output_size[0]) / float(self.crop_size[0])

        # create ground truth correspondence mask for flow between target prime and target
        mask_gt = create_border_mask(flow_gt_resized)
        mask_gt = mask_gt.bool() if float(torch.__version__[:3]) >= 1.1 else mask_gt.byte()

        if self.compute_mask_zero_borders:
            # if mask_gt has too little commun areas, overwrite to use that mask in anycase
            if mask_gt.sum() < mask_gt.shape[-1] * mask_gt.shape[-2] * self.min_percent_valid_corr:
                mask = mask_gt
            else:
                # mask black borders that might have appeared from the warping, when creating target_image_prime
                mask = define_mask_zero_borders(target_image_prime_resized)
            mini_batch['mask_zero_borders'] = mask

        if 'target_kps' in mini_batch.keys():
            mini_batch['target_kps'] = target_kp  # b, N, 2
            mini_batch['source_kps'] = source_kp  # b, N, 2

        if 'flow_map' in list(mini_batch.keys()):
            # gt between target and source (what we are trying to estimate during training unsupervised)
            mini_batch['flow_map_target_to_source'] = flow_gt_target_to_source
            mini_batch['correspondence_mask_target_to_source'] = mask_gt_target_to_source

        # save the new batch information
        mini_batch['source_image'] = source_image_resized.byte()
        mini_batch['target_image'] = target_image_resized.byte()
        mini_batch['target_image_prime'] = target_image_prime_resized.byte()  # if apply transfo after
        mini_batch['correspondence_mask'] = mask_gt
        mini_batch['flow_map'] = flow_gt_resized  # between target_prime and target, replace the old one
        return mini_batch


class BatchedImageTripletCreation2Flows:
    """ Class responsible for creating the image triplet used in the warp-consistency graph, from a pair of source and
    target images.
    Particularly, from the source and target image pair (which are usually not related by a known ground-truth flow),
    it creates an image triplet (source, target and target_prime) where the target prime image is related to the
    target by a known synthetically and randomly generated flow field (the flow generator is specified by user
    with argument 'synthetic_flow_generator').
    The final image triplet is obtained by cropping central patches of the desired dimensions in the three images.
    Here, two image triplets are actually created with different synthetic flow fields, resulting in different
    target_prime images, to apply different losses on each. The tensors corresponding to the second image triplet
    have the suffix '_ss' at the end of all fieldnames.
    """

    def __init__(self, settings, synthetic_flow_generator_for_unsupervised,
                 synthetic_flow_generator_for_self_supervised, compute_mask_zero_borders=False,
                 min_percent_valid_corr=0.1, crop_size=256, output_size=256, padding_mode='zeros'):
        """
        Args:
            settings: settings
            synthetic_flow_generator_for_unsupervised: class responsible for generating a synthetic flow field.
            synthetic_flow_generator_for_self_supervised: class responsible for generating a synthetic flow field.
            compute_mask_zero_borders: compute the mask of zero borders in target prime image? will be equal to 0
                                       where the target prime image is 0, 1 otherwise.
            min_percent_valid_corr: minimum percentage of matching regions between target prime and target. Otherwise,
                                    use ground-truth correspondence mask.
            crop_size: size of the center crop .
            output_size: size of the final outputted images and flow fields (resized after the crop).
        """

        self.synthetic_flow_generator_for_unsupervised = synthetic_flow_generator_for_unsupervised
        self.synthetic_flow_generator_for_self_supervised = synthetic_flow_generator_for_self_supervised
        
        self.device = getattr(settings, 'device', None)
        if self.device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() and settings.use_gpu else "cpu")

        if not isinstance(crop_size, tuple):
            crop_size = (crop_size, crop_size)
        self.crop_size = crop_size

        if not isinstance(output_size, tuple):
            output_size = (output_size, output_size)
        self.output_size = output_size
        self.min_percent_valid_corr = min_percent_valid_corr
        self.compute_mask_zero_borders = compute_mask_zero_borders
        self.padding_mode = padding_mode

    def compute_correspondence_mask(self, flow_gt_resized, target_image_prime_resized):
        # compute mask gt
        # create ground truth mask (for eval at least)
        mask_gt = create_border_mask(flow_gt_resized)
        mask_gt = mask_gt.bool() if float(torch.__version__[:3]) >= 1.1 else mask_gt.byte()

        mask = None
        if self.compute_mask_zero_borders:
            # if mask_gt is all zero (no commun areas), overwrite to use the mask in anycase
            if mask_gt.sum() < mask_gt.shape[-1] * mask_gt.shape[-2] * self.min_percent_valid_corr:
                mask = mask_gt
            else:
                # mask black borders that might have appeared from the warping
                occ_mask = target_image_prime_resized[:, 0, :, :].le(1e-8) & \
                           target_image_prime_resized[:, 1, :, :].le(1e-8) & \
                           target_image_prime_resized[:, 2, :, :].le(1e-8)
                mask = ~occ_mask
        return mask, mask_gt

    def __call__(self, mini_batch, grid=None, net=None, training=True,  *args, **kwargs):
        """
        Args:
            mini_batch: The mini batch input data, should at least contain the fields 'source_image', 'target_image'
            training: bool indicating if we are in training or evaluation mode
            net: network
        Returns:
            mini_batch: output data block with at least the fields 'source_image', 'target_image',
                        'target_image_prime', 'flow_map', 'correspondence_mask',
                        'target_image_prime_ss', 'flow_map_ss', 'correspondence_mask_ss'.
                        if self.compute_mask_zero_borders, will contain field 'mask_zero_borders', 'mask_zero_borders_ss',
                        If ground-truth is known (was provided) between source and target image, will contain the fields
                        'flow_map_target_to_source', 'correspondence_mask_target_to_source'.
        """

        # take original images
        source_image = mini_batch['source_image'].to(self.device)
        target_image = mini_batch['target_image'].to(self.device)
        b, _, h, w = source_image.shape

        if h <= self.output_size[0] or w <= self.output_size[1]:
            print('Image or flow is the same size than desired output size in warping dataset ! ')

        # get synthetic homography transformation from the synthetic flow generator
        # flow_gt_for_unsupervised here is between target prime and target
        flow_gt_for_unsupervised = self.synthetic_flow_generator_for_unsupervised(mini_batch=mini_batch,
                                                                                  training=training, net=net).detach()
        bs, _, h_f, w_f = flow_gt_for_unsupervised.shape
        if h_f != h or w_f != w:
            # reshape and rescale the flow so it has the load_size of the original images
            flow_gt_for_unsupervised = F.interpolate(flow_gt_for_unsupervised, (h, w),
                                                     mode='bilinear', align_corners=False)
            flow_gt_for_unsupervised[:, 0] *= float(w) / float(w_f)
            flow_gt_for_unsupervised[:, 1] *= float(h) / float(h_f)
        target_image_prime_for_unsupervised = warp(target_image, flow_gt_for_unsupervised,
                                                   padding_mode=self.padding_mode).byte()

        # for self-supervised
        flow_gt_for_self_supervised = self.synthetic_flow_generator_for_self_supervised(mini_batch=mini_batch,
                                                                                        training=training, net=net).detach()
        bs, _, h_f, w_f = flow_gt_for_self_supervised.shape
        if h_f != h or w_f != w:
            # reshape and rescale the flow so it has the load_size of the original images
            flow_gt_for_self_supervised = F.interpolate(flow_gt_for_self_supervised, (h, w), mode='bilinear',
                                                        align_corners=False)
            flow_gt_for_self_supervised[:, 0] *= float(w) / float(w_f)
            flow_gt_for_self_supervised[:, 1] *= float(h) / float(h_f)
        target_image_prime_for_self_supervised = warp(target_image, flow_gt_for_self_supervised).byte()

        # flow between source and target if it exists
        if 'flow_map' in list(mini_batch.keys()):
            flow_gt_target_to_source = mini_batch['flow_map'].to(self.device)
            mask_gt_target_to_source = mini_batch['correspondence_mask'].to(self.device)
        else:
            flow_gt_target_to_source = None
            mask_gt_target_to_source = None

        # crop a center patch from the images and the ground-truth flow field, so that black borders are removed
        x_start = w // 2 - self.crop_size[1] // 2
        y_start = h // 2 - self.crop_size[0] // 2
        source_image_resized = source_image[:, :, y_start: y_start + self.crop_size[0],
                                            x_start: x_start + self.crop_size[1]]
        target_image_resized = target_image[:, :, y_start: y_start + self.crop_size[0],
                                            x_start: x_start + self.crop_size[1]]
        target_image_prime_for_unsupervised_resized = target_image_prime_for_unsupervised\
            [:, :, y_start: y_start + self.crop_size[0], x_start: x_start + self.crop_size[1]]
        flow_gt_for_unsupervised_resized = flow_gt_for_unsupervised[:, :, y_start: y_start + self.crop_size[0],
                                           x_start: x_start + self.crop_size[1]]
        target_image_prime_for_self_supervised_resized = target_image_prime_for_self_supervised\
            [:, :, y_start: y_start + self.crop_size[0], x_start: x_start + self.crop_size[1]]
        flow_gt_for_self_supervised_resized = flow_gt_for_self_supervised[:, :, y_start: y_start + self.crop_size[0],
                                                                          x_start: x_start + self.crop_size[1]]

        if 'flow_map' in list(mini_batch.keys()):
            flow_gt_target_to_source = flow_gt_target_to_source[:, :, y_start: y_start + self.crop_size[0],
                                                                x_start: x_start + self.crop_size[1]]
            if mask_gt_target_to_source is not None:
                mask_gt_target_to_source = mask_gt_target_to_source[:, y_start: y_start + self.crop_size[0],
                                                                    x_start: x_start + self.crop_size[1]]

        if 'flow_map_target_to_source' in mini_batch.keys():
            # just for sanity check
            mini_batch['flow_map_target_to_source'] = mini_batch['flow_map_target_to_source']\
                [:, :, y_start: y_start + self.crop_size[0], x_start: x_start + self.crop_size[1]]
            mini_batch['flow_map_source_to_target'] = mini_batch['flow_map_source_to_target']\
                [:, :, y_start: y_start + self.crop_size[0], x_start: x_start + self.crop_size[1]]

        if 'target_kps' in mini_batch.keys():
            target_kp = mini_batch['target_kps'].to(self.device).clone()  # b, N, 2
            source_kp = mini_batch['source_kps'].to(self.device).clone()  # b, N, 2
            source_kp[:, :, 0] = source_kp[:, :, 0] - x_start   # will just make the not valid part even smaller
            source_kp[:, :, 1] = source_kp[:, :, 1] - y_start
            target_kp[:, :, 0] = target_kp[:, :, 0] - x_start
            target_kp[:, :, 1] = target_kp[:, :, 1] - y_start

        # resize to final outptu load_size, this is to prevent the crop from removing all common areas
        if self.output_size != self.crop_size:
            source_image_resized = F.interpolate(source_image_resized, self.output_size,
                                                 mode='area')
            target_image_resized = F.interpolate(target_image_resized, self.output_size,
                                                 mode='area')
            target_image_prime_for_unsupervised_resized = F.interpolate(target_image_prime_for_unsupervised_resized,
                                                                        self.output_size,
                                                                        mode='area')
            flow_gt_for_unsupervised_resized = F.interpolate(flow_gt_for_unsupervised_resized,
                                                             self.output_size,
                                                             mode='bilinear', align_corners=False)
            flow_gt_for_unsupervised_resized[:, 0] *= float(self.output_size[1]) / float(self.crop_size[1])
            flow_gt_for_unsupervised_resized[:, 1] *= float(self.output_size[0]) / float(self.crop_size[0])

            target_image_prime_for_self_supervised_resized = F.interpolate(target_image_prime_for_self_supervised_resized,
                                                                           self.output_size,
                                                                           mode='area')
            flow_gt_for_self_supervised_resized = F.interpolate(flow_gt_for_self_supervised_resized,
                                                                self.output_size,
                                                                mode='bilinear', align_corners=False)
            flow_gt_for_self_supervised_resized[:, 0] *= float(self.output_size[1]) / float(self.crop_size[1])
            flow_gt_for_self_supervised_resized[:, 1] *= float(self.output_size[0]) / float(self.crop_size[0])

            if 'flow_map' in list(mini_batch.keys()):
                flow_gt_target_to_source = F.interpolate(flow_gt_target_to_source, self.output_size,
                                                         mode='bilinear', align_corners=False)
                flow_gt_target_to_source[:, 0] *= float(self.output_size[1]) / float(self.crop_size[1])
                flow_gt_target_to_source[:, 1] *= float(self.output_size[0]) / float(self.crop_size[0])
                if mask_gt_target_to_source is not None:
                    mask_gt_target_to_source = F.interpolate(mask_gt_target_to_source.unsqueeze(1).float(),
                                                             self.output_size,
                                                             mode='bilinear', align_corners=False)
                    mask_gt_target_to_source = mask_gt_target_to_source.bool() if float(torch.__version__[:3]) >= 1.1 \
                        else mask_gt_target_to_source.byte()

            if 'target_kps' in mini_batch.keys():
                source_kp[:, :, 0] *= float(self.output_size[1]) / float(self.crop_size[1])
                source_kp[:, :, 1] *= float(self.output_size[0]) / float(self.crop_size[0])
                target_kp[:, :, 0] *= float(self.output_size[1]) / float(self.crop_size[1])
                target_kp[:, :, 1] *= float(self.output_size[0]) / float(self.crop_size[0])

        mask_for_unsupervised, mask_gt_for_unsupervised = self.compute_correspondence_mask(
            flow_gt_for_unsupervised_resized, target_image_prime_for_unsupervised_resized)

        mask_for_self_supervised, mask_gt_for_self_supervised = self.compute_correspondence_mask(
            flow_gt_for_self_supervised_resized, target_image_prime_for_self_supervised_resized)

        if 'flow_map' in list(mini_batch.keys()):
            # gt between target and source (what we are trying to estimate during training unsupervised)
            mini_batch['flow_map_target_to_source'] = flow_gt_target_to_source
            mini_batch['correspondence_mask_target_to_source'] = mask_gt_target_to_source

        if 'target_kps' in mini_batch.keys():
            mini_batch['target_kps'] = target_kp  # b, N, 2
            mini_batch['source_kps'] = source_kp  # b, N, 2

        # save the new batch information
        mini_batch['source_image'] = source_image_resized.byte()
        mini_batch['target_image'] = target_image_resized.byte()

        # for unsupervised estimation
        mini_batch['target_image_prime'] = target_image_prime_for_unsupervised_resized
        mini_batch['correspondence_mask'] = mask_gt_for_unsupervised
        if self.compute_mask_zero_borders:
            mini_batch['mask_zero_borders'] = mask_for_unsupervised  # just to be used for GLUNet
        # flow map gt between target_prime and target, replace the old one
        mini_batch['flow_map'] = flow_gt_for_unsupervised_resized

        # for self-supervised estimation
        mini_batch['target_image_prime_ss'] = target_image_prime_for_self_supervised_resized
        mini_batch['correspondence_mask_ss'] = mask_gt_for_self_supervised
        if self.compute_mask_zero_borders:
            mini_batch['mask_zero_borders_ss'] = mask_for_self_supervised  # just to be used for GLUNet
        # flow map gt between target_prime and target, replace the old one
        mini_batch['flow_map_ss'] = flow_gt_for_self_supervised_resized
        return mini_batch
