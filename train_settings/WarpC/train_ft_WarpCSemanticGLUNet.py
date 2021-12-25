
from termcolor import colored
import torch.optim as optim
import torchvision.transforms as transforms
import torch.optim.lr_scheduler as lr_scheduler
from training.actors.warp_consistency_actor_GLUNet import GLUNetWarpCUnsupervisedBatchPreprocessing, GLUNetWarpCUnsupervisedActor
from training.losses.basic_losses import L1
from training.losses.multiscale_loss import MultiScaleFlow
from training.trainers.matching_trainer import MatchingTrainer
from utils_data.loaders import Loader
from admin.multigpu import MultiGPU
from utils_data.image_transforms import ArrayToTensor
from training.actors.warp_consistency_utils.online_triplet_creation import BatchedImageTripletCreation
from training.actors.warp_consistency_utils.synthetic_flow_generation_from_pair_batch import \
    GetRandomSyntheticAffHomoTPSFlow, SynthecticAffHomoTPSTransfo
from datasets.semantic_matching_datasets.pfpascal import PFPascalDataset
import torch
import numpy as np
import os
from models.GLUNet.Semantic_GLUNet import SemanticGLUNetModel
from utils_data.augmentations.color_augmentation_torch import ColorJitter, RandomGaussianBlur
from utils_data.euler_wrapper import prepare_data


def run(settings):
    settings.description = 'Default train settings for finetuning SemanticGLU-Net with Warp Consistency'
    settings.data_mode = 'euler'
    settings.batch_size = 4  # 5 fit in 1 GPU with 11 G
    settings.n_threads = 8
    settings.multi_gpu = True
    settings.print_interval = 300
    settings.lr = 0.00008
    settings.n_epochs = 70
    settings.step_size_scheduler = [40, 50, 60]
    settings.initial_pretrained_model = os.path.join(settings.env.pre_trained_models_dir,
                                                     'GLUNet/SemanticGLUNet_CityScape_DPED_ADE.pth')

    # specific training parameters
    # loss applied in non-black target prime regions (to account for warping). It is very important, if applied to
    # black regions, weird behavior. If valid mask, doesn't learn interpolation in non-visibile regions.
    settings.compute_mask_zero_borders = True
    settings.apply_mask = False  # valid visible matches, we apply mask_zero_borders instead
    settings.nbr_plot_images = 1

    # loss parameters
    settings.name_of_loss = 'warp_supervision_and_w_bipath'  # the warp consistency objective
    settings.compute_visibility_mask = True
    settings.apply_constant_flow_weight = False
    settings.loss_weight = {'warp_supervision': 1.0, 'w_bipath': 1.0,
                            'warp_supervision_constant': 1.0, 'w_bipath_constant': 1.0,
                            'cc_mask_alpha_1': 0.03, 'cc_mask_alpha_2': 0.5}

    # transfo parameters
    settings.resizing_size = 500
    settings.crop_size = 400  # reduced size, so it does not take too much memory
    settings.parametrize_with_gaussian = False
    settings.transformation_types = ['hom', 'tps', 'afftps']
    settings.random_t = 0.25
    settings.random_s = 0.4
    settings.random_alpha = np.pi / 12
    settings.random_t_tps_for_afftps = 100.0 / float(settings.resizing_size)
    settings.random_t_hom = 100.0 / float(settings.resizing_size)
    settings.random_t_tps = 100.0 / float(settings.resizing_size)
    settings.appearance_transfo_target_prime = transforms.Compose([ColorJitter(brightness=0.6, contrast=0.6,
                                                                               saturation=0.6, hue=0.5 / 3.14),
                                                                   RandomGaussianBlur(sigma=(0.2, 2.0),
                                                                                      probability=0.2)])

    # apply pre-processing to the images
    # here pre-processing is done within the function
    flow_transform = transforms.Compose([ArrayToTensor()])  # just put channels first and put it to float
    image_transforms = transforms.Compose([ArrayToTensor(get_float=True)])  # just put channels first

    # base dataset to get target and source, real image pair. Here, we do not use the annotations during training.
    prepare_data(settings.env.PFPascal_tar, mode=settings.data_mode)
    pascal_cfg = {'augment_with_crop': False, 'crop_size': [settings.resizing_size, settings.resizing_size],
                  'augment_with_flip': False, 'proba_of_image_flip': 0.0, 'proba_of_batch_flip': 0.5,
                  'output_image_size': [settings.resizing_size, settings.resizing_size],
                  'pad_to_same_shape': False, 'output_flow_size': [settings.resizing_size, settings.resizing_size]}
    train_dataset = PFPascalDataset(root=settings.env.PFPascal, split='train', source_image_transform=image_transforms,
                                    target_image_transform=image_transforms, flow_transform=flow_transform,
                                    training_cfg=pascal_cfg)

    val_dataset = PFPascalDataset(root=settings.env.PFPascal, split='val', source_image_transform=image_transforms,
                                  target_image_transform=image_transforms, flow_transform=flow_transform,
                                  training_cfg=pascal_cfg)

    # dataloader
    train_loader = Loader('train', train_dataset, batch_size=settings.batch_size,
                          drop_last=True, training=True, num_workers=settings.n_threads)

    val_loader = Loader('val', val_dataset, batch_size=settings.batch_size, shuffle=False, drop_last=True,
                        epoch_interval=1.0, training=False, num_workers=settings.n_threads)

    # models
    model = SemanticGLUNetModel(batch_norm=True, pyramid_type='VGG', md=4, cyclic_consistency=False,
                                consensus_network=True, iterative_refinement=False)
    # if Load pre-trained weights !
    if settings.initial_pretrained_model:
        try:
            model.load_state_dict(torch.load(settings.initial_pretrained_model)['state_dict'])
        except:
            model.load_state_dict(torch.load(settings.initial_pretrained_model))
        print('Initialised weights')
    print(colored('==> ', 'blue') + 'model created.')

    # Wrap the network for multi GPU training
    if settings.multi_gpu:
        model = MultiGPU(model)

    # Loss module

    sample_transfo = SynthecticAffHomoTPSTransfo(size_output_flow=settings.resizing_size, random_t=settings.random_t,
                                                 random_s=settings.random_s,
                                                 random_alpha=settings.random_alpha,
                                                 random_t_tps_for_afftps=settings.random_t_tps_for_afftps,
                                                 random_t_hom=settings.random_t_hom, random_t_tps=settings.random_t_tps,
                                                 transformation_types=settings.transformation_types,
                                                 parametrize_with_gaussian=settings.parametrize_with_gaussian
                                                 )
    synthetic_flow_generator = GetRandomSyntheticAffHomoTPSFlow(settings=settings, transfo_sampling_module=sample_transfo,
                                                                size_output_flow=settings.resizing_size)

    triplet_creator = BatchedImageTripletCreation(settings, synthetic_flow_generator=synthetic_flow_generator,
                                                  compute_mask_zero_borders=settings.compute_mask_zero_borders,
                                                  output_size=settings.crop_size, crop_size=settings.crop_size)

    batch_processing = GLUNetWarpCUnsupervisedBatchPreprocessing(
        settings, apply_mask=settings.apply_mask,
        apply_mask_zero_borders=settings.compute_mask_zero_borders, online_triplet_creator=triplet_creator,
        appearance_transform_source=None, appearance_transform_target=None,
        appearance_transform_target_prime=settings.appearance_transfo_target_prime)

    # loss
    objective = L1()
    weights_level_loss = [0.32, 0.08, 0.02, 0.01]
    loss_module_256 = MultiScaleFlow(level_weights=weights_level_loss[:2], loss_function=objective,
                                     downsample_gt_flow=True)
    loss_module = MultiScaleFlow(level_weights=weights_level_loss[2:], loss_function=objective,
                                 downsample_gt_flow=True)

    # actor
    glunet_actor = GLUNetWarpCUnsupervisedActor(model, objective=loss_module, objective_256=loss_module_256,
                                                batch_processing=batch_processing, loss_weight=settings.loss_weight,
                                                name_of_loss=settings.name_of_loss, best_val_epe=True,
                                                compute_visibility_mask=settings.compute_visibility_mask,
                                                nbr_images_to_plot=settings.nbr_plot_images, semantic_evaluation=True)

    # Optimizer
    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=settings.lr, weight_decay=0.0004)

    # Scheduler
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=settings.step_size_scheduler, gamma=0.5)

    trainer = MatchingTrainer(glunet_actor, [train_loader, val_loader], optimizer, settings, lr_scheduler=scheduler,
                              make_initial_validation=True)

    trainer.train(settings.n_epochs, load_latest=True, fail_safe=True)




