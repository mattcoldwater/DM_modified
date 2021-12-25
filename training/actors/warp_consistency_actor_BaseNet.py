import torch
from training.losses.basic_losses import realEPE, real_metrics
from .base_actor import BaseActor
import torch
import torch.nn.functional as F
from utils_flow.flow_and_mapping_operations import unormalise_and_convert_mapping_to_flow
from training.plot.plot_warp_consistency import plot_flows_warpc
import os
from admin.multigpu import is_multi_gpu
from training.losses.warp_consistency_losses import WBipathLoss, weights_self_supervised_and_unsupervised
from .warp_consistency_utils.mask_strategies import get_mask
from .batch_processing import normalize_image_with_imagenet_weights
from admin.stats import merge_dictionaries


class GetBaseNetPredictions:
    def __call__(self, source_image, target_image, net, device, pre_process_data, *args, **kwargs):
        net.eval()
        # torch.with_no_grad:
        b, _, h, w = target_image.shape
        if pre_process_data:
            source_image = normalize_image_with_imagenet_weights(source_image)
            target_image = normalize_image_with_imagenet_weights(target_image)
        estimated_flow_target_to_source = net(target_image, source_image)['flow_estimates'][-1]
        estimated_flow_source_to_target = net(source_image, target_image)['flow_estimates'][-1]
        estimated_flow_target_to_source = F.interpolate(estimated_flow_target_to_source, (h, w),
                                                        mode='bilinear', align_corners=False)
        estimated_flow_source_to_target = F.interpolate(estimated_flow_source_to_target, (h, w),
                                                        mode='bilinear', align_corners=False)
        return estimated_flow_target_to_source, estimated_flow_source_to_target


class GLOCALNetWarpCUnsupervisedBatchPreprocessing:
    """ Class responsible for processing the mini-batch to create the desired training inputs for GLOCALNet
     based networks, when training unsupervised using warp consistency.
     Particularly, from the source and target image pair (which are usually not related by a known ground-truth flow),
     the online_triplet_creator creates an image triplet (source, target and target_prime) where the target prime
     image is related to the target by a known synthetically and randomly generated flow field.
     The final image triplet is obtained by cropping central patches in the three images.
     Here, appearance augmentations can optionally be added, the images are normalized when necessary and the
     ground-truth flow field as well as mask used for training are processed.
     In that case, the ground-truth flow field used during training is the synthetic flow field relating
     target prime to target.
    """
    def __init__(self, settings, apply_mask=False, apply_mask_zero_borders=False, mask_strategy=None,
                 mapping=False, online_triplet_creator=None, normalize_images=False, window_center_mask=70,
                 appearance_transform_source=None, appearance_transform_target=None, appearance_transform_target_prime=None):
        """
        Args:
            settings: settings
            apply_mask: apply ground-truth correspondence mask for loss computation?
            apply_mask_zero_borders: apply mask zero borders (equal to 0 at black borders in target image) for loss
                                     computation?
            mapping: load correspondence map instead of flow field?
            online_triplet_creator: class responsible for the creation of the image triplet used in the warp
                                    consistency graph, from an image pair.
            normalize_images: bool, indicating if need to normalize images with ImageNet weights.
            appearance_transform_source: appearance augmentation applied to batched source image tensors
                                        (before normalizing)
            appearance_transform_target: appearance augmentation applied to batched target image tensors
                                        (before normalizing)
            appearance_transform_target_prime: appearance augmentation applied to batched target prime image tensors
                                        (before normalizing)
        """
        self.apply_mask = apply_mask
        self.apply_mask_zero_borders = apply_mask_zero_borders
        self.mask_strategy = mask_strategy
        self.window_center_mask = window_center_mask
        self.mapping = mapping

        self.device = getattr(settings, 'device', None)
        if self.device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.online_triplet_creator = online_triplet_creator

        self.appearance_transform_source = appearance_transform_source
        self.appearance_transform_target = appearance_transform_target
        self.appearance_transform_target_prime = appearance_transform_target_prime

        self.normalize_images = normalize_images

    def __call__(self, mini_batch, net=None, training=False, *args, **kwargs):
        """
        Args:
            mini_batch: The mini batch input data, should at least contain the fields 'source_image', 'target_image'
            training: bool indicating if we are in training or evaluation mode
            net: network
        Returns:
            mini_batch: output data block with at least the fields 'source_image', 'target_image',
                        'target_image_prime', 'flow_map', 'mask', 'correspondence_mask'.
                         If ground-truth is known between source and target image, will contain the fields
                        'flow_map_target_to_source', 'correspondence_mask_target_to_source'.
        """

        # create the image triplet from the image pair
        if self.online_triplet_creator is not None:
            mini_batch = self.online_triplet_creator(mini_batch, train=training, net=net)

        # add appearance augmentation to all three images
        if self.appearance_transform_source is not None:
            mini_batch['source_image'] = self.appearance_transform_source(mini_batch['source_image'])

        if self.appearance_transform_target is not None:
            mini_batch['target_image'] = self.appearance_transform_target(mini_batch['target_image'])

        if self.appearance_transform_target_prime is not None:
            mini_batch['target_image_prime'] = self.appearance_transform_target_prime(mini_batch['target_image_prime'])

        # update mini_batch with new images (also put to gpu) and normalize them if necessary
        mini_batch['source_image'] = mini_batch['source_image'].to(self.device)
        mini_batch['target_image'] = mini_batch['target_image'].to(self.device)
        mini_batch['target_image_prime'] = mini_batch['target_image_prime'].to(self.device)

        if self.normalize_images:
            mini_batch['source_image'] = normalize_image_with_imagenet_weights(mini_batch['source_image'])
            mini_batch['target_image'] = normalize_image_with_imagenet_weights(mini_batch['target_image'])
            mini_batch['target_image_prime'] = normalize_image_with_imagenet_weights(mini_batch['target_image_prime'])

        if self.mapping:
            mapping_gt = mini_batch['correspondence_map_pyro'][-1].to(self.device)
            flow_gt = unormalise_and_convert_mapping_to_flow(mapping_gt.permute(0,3,1,2))
        else:
            flow_gt = mini_batch['flow_map'].to(self.device)
        if flow_gt.shape[1] != 2:
            flow_gt.permute(0, 3, 1, 2)
        bs, _, h, w = flow_gt.shape

        mask = None

        if self.mask_strategy is not None:
            mask = get_mask(self.mask_strategy, mini_batch, self.window_center_mask).to(self.device)
        else:
            # the default
            if self.apply_mask_zero_borders:
                if 'mask_zero_borders' not in mini_batch.keys():
                    raise ValueError('Mask zero borders not in mini batch, check arguments to triplet creator')
                mask = mini_batch['mask_zero_borders'].to(self.device)
            elif self.apply_mask:
                mask = mini_batch['correspondence_mask'].to(self.device)

        if mask is not None and (mask.shape[1] != h or mask.shape[2] != w):
            # mask_gt does not have the proper shape
            mask = F.interpolate(mask.float().unsqueeze(1), (h, w), mode='bilinear',
                                 align_corners=False).squeeze(1).byte()  # bxhxw
        mask = mask.bool() if float(torch.__version__[:3]) >= 1.1 else mask.byte()

        mini_batch['correspondence_mask'] = mini_batch['correspondence_mask'].to(self.device)
        mini_batch['mask'] = mask
        mini_batch['flow_map'] = flow_gt  # between target_prime and target

        # if ground-truth between source and target exists, will be used for validation
        if 'flow_map_target_to_source' in list(mini_batch.keys()):
            # gt between target and source (what we are trying to estimate during training unsupervised)
            mini_batch['flow_map_target_to_source'] = mini_batch['flow_map_target_to_source'].to(self.device)
            mini_batch['correspondence_mask_target_to_source'] = \
                mini_batch['correspondence_mask_target_to_source'].to(self.device)
        return mini_batch


class GLOCALNetWarpCUnsupervisedActor(BaseActor):
    """Actor for training unsupervised GLOCALNet based networks with the warp consistency objective."""
    def __init__(self, net, objective, batch_processing, name_of_loss, loss_weight=None, detach_flow_for_warping=True,
                 nbr_images_to_plot=1, apply_constant_flow_weights=False, compute_visibility_mask=False,
                 semantic_evaluation=False):
        """
        Args:
            net: The network to train
            objective: The loss function
            batch_processing: A processing class which performs the necessary processing of the batched data.
            name_of_loss: 'warp_supervision_and_w_bipath' or 'w_bipath' or 'warp_supervision'
            loss_weight: weights used to balance w_bipath and warp_supervision.
            apply_constant_flow_weights: bool, otherwise, balance both losses according to given weights.
            detach_flow_for_warping: bool
            compute_visibility_mask: bool
            nbr_images_to_plot: number of images to plot per epoch
            semantic_evaluation: bool, to adapt the thresholds used in the PCK computations
        """
        super().__init__(net, objective, batch_processing)
        loss_weight_default = {'w_bipath': 1.0, 'warp_supervision': 1.0,
                               'w_bipath_constant': 1.0, 'warp_supervision_constant': 1.0,
                               'cc_mask_alpha_1': 0.01, 'cc_mask_alpha_2': 0.5}
        self.loss_weight = loss_weight_default
        if loss_weight is not None:
            self.loss_weight.update(loss_weight)
        self.batch_processing = batch_processing
        self.semantic_evaluation = semantic_evaluation

        self.name_of_loss = name_of_loss
        self.compute_visibility_mask = compute_visibility_mask
        # define the loss computation modules
        self.apply_constant_flow_weights = apply_constant_flow_weights

        if 'w_bipath' in name_of_loss:
            self.unsupervised_objective = WBipathLoss(
                objective, loss_weight, detach_flow_for_warping,
                compute_cyclic_consistency=self.compute_visibility_mask, alpha_1=self.loss_weight['cc_mask_alpha_1'],
                alpha_2=self.loss_weight['cc_mask_alpha_2'])
        elif 'warp_supervision' not in name_of_loss:
            raise ValueError('The name of the loss is not correct, you chose {}'.format(self.name_of_loss))

        self.nbr_images_to_plot = nbr_images_to_plot

    def __call__(self, mini_batch, training):
        """
        args:
            mini_batch: The mini batch input data, should at least contain the fields 'source_image', 'target_image',
                        'target_image_prime', 'flow_map', 'mask', 'correspondence_mask'.
                         If ground-truth is known between source and target image, will contain the fields
                        'flow_map_target_to_source', 'correspondence_mask_target_to_source'.
            training: bool indicating if we are in training or evaluation mode
        returns:
            loss: the training loss
            stats: dict containing detailed losses
        """
        # Run network
        epoch = mini_batch['epoch']
        iter = mini_batch['iter']
        mini_batch = self.batch_processing(mini_batch, net=self.net, training=training)
        b, _, h, w = mini_batch['flow_map'].shape

        # extract features to avoid recomputing them
        net = self.net.module if is_multi_gpu(self.net) else self.net
        im_source_pyr, im_target_pyr, im_target_prime_pyr = None, None, None
        if hasattr(net, 'pyramid'):
            im_source_pyr = net.pyramid(mini_batch['source_image'])
            im_target_pyr = net.pyramid(mini_batch['target_image'])
            im_target_prime_pyr = net.pyramid(mini_batch['target_image_prime'])

        # compute flows
        if not training or iter < self.nbr_images_to_plot:
            estimated_flow_target_to_source = self.net(target_image=mini_batch['target_image'],
                                                       source_image=mini_batch['source_image'],
                                                       im_target_pyr=im_target_pyr,
                                                       im_source_pyr=im_source_pyr)['flow_estimates']

        estimated_flow_target_prime_to_target_directly = None
        if not training or 'warp_supervision' in self.name_of_loss or iter < self.nbr_images_to_plot:
            estimated_flow_target_prime_to_target_directly = self.net(target_image=mini_batch['target_image_prime'],
                                                                      source_image=mini_batch['target_image'],
                                                                      im_target_pyr=im_target_prime_pyr,
                                                                      im_source_pyr=im_target_pyr)['flow_estimates']

        loss_ss, un_loss = 0.0, 0.0
        stats_ss, un_stats = {}, {}
        if 'warp_supervision' in self.name_of_loss:
            loss_ss, stats_ss = self.objective(estimated_flow_target_prime_to_target_directly, mini_batch['flow_map'],
                                         mask=mini_batch['mask'])

            # log stats
            output_un = {'estimated_flow_target_prime_to_target_through_composition':
                                estimated_flow_target_prime_to_target_directly}

        if 'w_bipath' in self.name_of_loss:
            estimated_flow_target_prime_to_source = self.net(target_image=mini_batch['target_image_prime'],
                                                             source_image=mini_batch['source_image'],
                                                             im_target_pyr=im_target_prime_pyr,
                                                             im_source_pyr=im_source_pyr)['flow_estimates']
            estimated_flow_source_to_target = self.net(target_image=mini_batch['source_image'],
                                                       source_image=mini_batch['target_image'],
                                                       im_target_pyr=im_source_pyr,
                                                       im_source_pyr=im_target_pyr)['flow_estimates']

            un_loss, un_stats, output_un = self.unsupervised_objective(mini_batch['flow_map'], mini_batch['mask'],
                                                                       estimated_flow_target_prime_to_source,
                                                                       estimated_flow_source_to_target)

            # compute self-supervised part of the loss
        if self.name_of_loss == 'warp_supervision':
            stats = stats_ss
            stats['Loss/total'] = loss_ss.item()
            loss = loss_ss
        elif self.name_of_loss == 'w_bipath':
            loss = un_loss
            stats = un_stats
            stats['Loss/total'] = loss.item()
        else:
            ss_loss, ss_stats = self.objective(estimated_flow_target_prime_to_target_directly,
                                               mini_batch['flow_map'], mask=mini_batch['mask'])

            # merge stats and losses
            stats = merge_dictionaries([un_stats, ss_stats], name=['w_bipath', 'warp_sup'])

            loss, stats = weights_self_supervised_and_unsupervised(ss_loss, un_loss, stats, self.loss_weight,
                                                                   self.apply_constant_flow_weights)

        # Calculates validation stats
        if not training:
            if 'flow_map_target_to_source' in list(mini_batch.keys()):
                mask_gt_target_to_source = mini_batch['correspondence_mask_target_to_source']
                flow_gt_target_to_source = mini_batch['flow_map_target_to_source']
                if self.semantic_evaluation:
                    thresh_1, thresh_2, thresh_3 = max(flow_gt_target_to_source.shape[-2:]) * 0.05, \
                    max(flow_gt_target_to_source.shape[-2:]) * 0.1, max(flow_gt_target_to_source.shape[-2:]) * 0.15
                else:
                    thresh_1, thresh_2, thresh_3 = 1.0, 3.0, 5.0
                for index_reso in range(len(estimated_flow_target_to_source)):
                    EPE, PCK_1, PCK_3, PCK_5 = real_metrics(estimated_flow_target_to_source[-(index_reso + 1)],
                                                            flow_gt_target_to_source, mask_gt_target_to_source,
                                                            thresh_1=thresh_1, thresh_2=thresh_2, thresh_3=thresh_3)

                    stats['EPE_target_to_source_reso_{}/EPE'.format(index_reso)] = EPE.item()
                    stats['PCK_{}_target_to_source_reso_{}/EPE'.format(thresh_1, index_reso)] = PCK_1.item()
                    stats['PCK_{}_target_to_source_reso_{}/EPE'.format(thresh_2, index_reso)] = PCK_3.item()
                    stats['PCK_{}_target_to_source_reso_{}/EPE'.format(thresh_3, index_reso)] = PCK_5.item()

            for index_reso in range(len(estimated_flow_target_prime_to_target_directly)):
                EPE = realEPE(estimated_flow_target_prime_to_target_directly[-(index_reso + 1)],
                              mini_batch['flow_map'], mini_batch['correspondence_mask'])
                stats['EPE_target_prime_to_target_reso_direct_{}/EPE'.format(index_reso)] = EPE.item()

            for index_reso in range(len(output_un['estimated_flow_target_prime_to_target_through_composition'])):
                EPE = realEPE(output_un['estimated_flow_target_prime_to_target_through_composition'][-(index_reso + 1)],
                              mini_batch['flow_map'], mini_batch['correspondence_mask'])

                stats['EPE_target_prime_to_target_reso_composition_{}/EPE'.format(index_reso)] = EPE.item()

            if 'flow_map_target_to_source' in list(mini_batch.keys()):
                stats['best_value'] = stats['EPE_target_to_source_reso_0/EPE']
            else:
                stats['best_value'] = stats['EPE_target_prime_to_target_reso_composition_0/EPE']

        # plot images
        if iter < self.nbr_images_to_plot:
            training_or_validation = 'train' if training else 'val'
            base_save_dir = os.path.join(mini_batch['settings'].env.workspace_dir, mini_batch['settings'].project_path,
                                         'plot', training_or_validation)
            if not os.path.isdir(base_save_dir):
                os.makedirs(base_save_dir)

            if self.name_of_loss == 'warp_supervision':
                plot_flows_warpc(base_save_dir, 'epoch{}_batch{}_reso_64'.format(epoch, iter), h, w,
                                 image_source=mini_batch['source_image'],
                                 image_target=mini_batch['target_image'],
                                 image_target_prime=mini_batch['target_image_prime'],
                                 estimated_flow_target_to_source=None,
                                 estimated_flow_target_prime_to_source=None,
                                 estimated_flow_target_prime_to_target=None,
                                 estimated_flow_target_prime_to_target_directly=
                                 estimated_flow_target_prime_to_target_directly[-1],
                                 gt_flow_target_prime_to_target=mini_batch['flow_map'],
                                 sparse=mini_batch['sparse'], mask=mini_batch['mask'])
            else:
                plot_flows_warpc(base_save_dir, 'epoch{}_batch{}_reso_64'.format(epoch, iter), h, w,
                                 image_source=mini_batch['source_image'],
                                 image_target=mini_batch['target_image'],
                                 image_target_prime=mini_batch['target_image_prime'],
                                 estimated_flow_target_to_source=estimated_flow_target_to_source[-1],
                                 estimated_flow_target_prime_to_source=estimated_flow_target_prime_to_source[-1],
                                 estimated_flow_target_prime_to_target=
                                 output_un['estimated_flow_target_prime_to_target_through_composition'][-1],
                                 estimated_flow_target_prime_to_target_directly=
                                 estimated_flow_target_prime_to_target_directly[-1],
                                 gt_flow_target_prime_to_target=mini_batch['flow_map'],
                                 gt_flow_target_to_source=mini_batch['flow_map_target_to_source'] if
                                 'flow_map_target_to_source' in list(mini_batch.keys()) else None,
                                 estimated_flow_source_to_target=estimated_flow_source_to_target[-1] if
                                 estimated_flow_source_to_target is not None else None,
                                 sparse=mini_batch['sparse'], mask=output_un['mask_training'][-1])
        return loss, stats









