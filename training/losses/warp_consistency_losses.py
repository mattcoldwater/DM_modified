import torch
import torch.nn.functional as F
from utils_flow.pixel_wise_mapping import warp


def weights_self_supervised_and_unsupervised(loss_su, loss_un, stats, loss_weight, apply_constant_weights=False):
    if not apply_constant_weights:
        L_supervised = loss_su.detach()
        L_unsupervised = loss_un.detach()
        ratio = loss_weight['warp_supervision'] / loss_weight['w_bipath']
        if L_unsupervised > L_supervised:
            u_l_w = 1
            s_l_w = L_unsupervised / (L_supervised + 1e-8) * ratio
        else:
            u_l_w = L_supervised / (L_unsupervised + 1e-8) / ratio
            s_l_w = 1
        loss = loss_un * u_l_w + loss_su * s_l_w
        stats['Loss_w_bipath/total'] = (loss_un * u_l_w).item()
        stats['Loss_warp_sup/total'] = (loss_su * s_l_w).item()
        stats['Loss/total'] = loss.item()
    else:
        loss = loss_weight['w_bipath_constant'] * loss_un + \
               loss_weight['warp_supervision_constant'] * loss_su
        stats['Loss_w_bipath/total'] = (loss_weight['w_bipath_constant'] * loss_un).item()
        stats['Loss_warp_sup/total'] = (loss_weight['warp_supervision_constant'] * loss_su).item()
        stats['Loss/total'] = loss.item()
    return loss, stats


def length_sq(x):
    return torch.sum(x**2, dim=1)


class WBipathLoss:
    """
    Main module computing the W-bipath loss. The W-bipath constraints computes the flow composition from the
    target prime to the target image.
    """
    def __init__(self, objective, loss_weight, detach_flow_for_warping=True, compute_cyclic_consistency=False,
                 alpha_1=0.01, alpha_2=0.5):
        """

        Args:
            objective: final objective, like multi-scale EPE or L1 loss
            loss_weight: weights used
            detach_flow_for_warping: bool, prevent back-propagation through the flow used for warping.
            compute_cyclic_consistency:
            alpha_1: hyper-parameter for the visibility mask
            alpha_2: hyper-parameter for the visibility mask
        """
        self.objective = objective
        self.loss_weight = loss_weight
        self.detach_flow_for_warping = detach_flow_for_warping

        self.compute_cyclic_consistency = compute_cyclic_consistency
        self.alpha_1 = alpha_1
        self.alpha_2 = alpha_2

    def get_cyclic_consistency_mask(self, estimated_flow_target_prime_to_source_per_level,
                                    warping_flow_source_to_target, synthetic_flow):

        b, _, h, w = synthetic_flow.shape
        b, _, h_, w_ = estimated_flow_target_prime_to_source_per_level.shape

        synthetic_flow = F.interpolate(synthetic_flow, (h_, w_), mode='bilinear', align_corners=False)

        # defines occluded pixels (or just flow is not good enough)
        mag_sq_fw = length_sq(estimated_flow_target_prime_to_source_per_level) + \
                    length_sq(warping_flow_source_to_target) + length_sq(synthetic_flow)
        occ_thresh_fw = self.alpha_1 * mag_sq_fw + self.alpha_2
        fb_occ_fw = length_sq(estimated_flow_target_prime_to_source_per_level + warping_flow_source_to_target -
                              synthetic_flow) > occ_thresh_fw

        # defines the mask of not occluded pixels
        mask_fw = ~fb_occ_fw  # shape bxhxw
        return mask_fw

    def __call__(self, flow_map, mask_used, estimated_flow_target_prime_to_source,
                 estimated_flow_source_to_target, *args, **kwargs):
        """

        Args:
            flow_map: corresponds to known flow relating the target prime image to the target
            mask_used: mask indicating in which regions the flow_map is valid
            estimated_flow_target_prime_to_source: list of estimated flows
            estimated_flow_source_to_target: list of estimated flows
        Returns:
            loss_un: final loss
            stats_un: stats from loss computation
            output: dictionary containing some intermediate results, for example the composition flow.
        """
        b, _, h, w = flow_map.shape  # load_size of the gt flow, meaning load_size of the input images

        output = {}
        estimated_flow_target_prime_to_target_through_composition = []
        masks = []
        if self.compute_cyclic_consistency:
            mask_cyclic_list = []

        if not isinstance(estimated_flow_target_prime_to_source, list):
            estimated_flow_target_prime_to_source = [estimated_flow_target_prime_to_source]
        if not isinstance(estimated_flow_source_to_target, list):
            estimated_flow_source_to_target = [estimated_flow_source_to_target]

        for nbr, (estimated_flow_target_prime_to_source_per_level, estimated_flow_source_to_target_per_level) \
                in enumerate(zip(estimated_flow_target_prime_to_source, estimated_flow_source_to_target)):
            b, _, h_, w_ = estimated_flow_target_prime_to_source_per_level.shape

            if self.detach_flow_for_warping:
                estimated_flow_target_prime_to_source_per_level_warping = \
                    estimated_flow_target_prime_to_source_per_level.detach() * 1.0
            else:
                estimated_flow_target_prime_to_source_per_level_warping = \
                    estimated_flow_target_prime_to_source_per_level * 1.0
            estimated_flow_target_prime_to_source_per_level_warping[:, 0, :, :] *= float(w_) / float(w)
            estimated_flow_target_prime_to_source_per_level_warping[:, 1, :, :] *= float(h_) / float(h)

            warping_flow_source_to_target = warp(estimated_flow_source_to_target_per_level,
                                                 estimated_flow_target_prime_to_source_per_level_warping)
            estimated_flow = estimated_flow_target_prime_to_source_per_level + warping_flow_source_to_target
            estimated_flow_target_prime_to_target_through_composition.append(estimated_flow)

            # need to also compute the mask according to warping
            mask = (warp(torch.ones(b, 1, h_, w_, requires_grad=False).cuda(),
                         estimated_flow_target_prime_to_source_per_level_warping.detach()).ge(0.2)).squeeze(1)
            mask = mask & F.interpolate(mask_used.unsqueeze(1).float(), (h_, w_), mode='bilinear',
                                        align_corners=False).ge(0.98).squeeze(1) if mask_used is not None else mask

            if self.compute_cyclic_consistency:
                mask_cyclic = self.get_cyclic_consistency_mask(estimated_flow_target_prime_to_source_per_level.detach(),
                                                               warping_flow_source_to_target.detach(), flow_map)
                mask = mask & mask_cyclic
                mask_cyclic_list.append(mask_cyclic)
            masks.append(mask)

        output['mask_training'] = masks
        if self.compute_cyclic_consistency:
            output['mask_cyclic'] = mask_cyclic_list
        output['estimated_flow_target_prime_to_target_through_composition'] = \
            estimated_flow_target_prime_to_target_through_composition

        loss_un, stats_un = self.objective(estimated_flow_target_prime_to_target_through_composition,
                                           flow_map, mask=masks)
        return loss_un, stats_un, output
