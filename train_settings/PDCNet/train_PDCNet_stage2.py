import torch.optim as optim
from termcolor import colored
import torchvision.transforms as transforms
import torch.optim.lr_scheduler as lr_scheduler
from utils_data.image_transforms import ArrayToTensor
from training.actors.batch_processing import GLUNetBatchPreprocessing
from training.losses.neg_log_likelihood import NLLMixtureLaplace
from training.losses.multiscale_loss import MultiScaleMixtureDensity
from training.trainers.matching_trainer import MatchingTrainer
from utils_data.loaders import Loader
from admin.multigpu import MultiGPU
from training.actors.self_supervised_actor import GLUNetBasedActor
from datasets.object_augmented_dataset import MSCOCO, AugmentedImagePairsDatasetMultipleObjects
from models.PDCNet.PDCNet import PDCNet_vgg16
from datasets.load_pre_made_datasets.load_pre_made_dataset import PreMadeDataset
from datasets.object_augmented_dataset.synthetic_object_augmentation_for_pairs_multiple_ob import RandomAffine
from datasets.MegaDepth.megadepth import MegaDepthDataset
from datasets.mixture_of_datasets import MixDatasets
import os
import torch
from utils_data.sampler import RandomSampler
from admin.loading import partial_load


def run(settings):
    settings.description = 'Default train settings for PDC-Net stage 1'
    settings.data_mode = 'euler'
    settings.batch_size = 3  # train on 2 GPU of 24GB
    settings.n_threads = 8
    settings.multi_gpu = True
    settings.print_interval = 500
    settings.lr = 0.00005
    settings.lr_feature_backbone = 1e-5
    settings.scheduler_steps = [40, 60, 80]
    settings.n_epochs = 100
    settings.dataset_callback_fn = 'sample_new_items'  # use to resample image pair at each epoch
    # initialize with PDCNet stage 1 model
    settings.initial_pretrained_model = os.path.join(settings.env.workspace_dir,
                                                     'train_settings/PDCNet/train_PDCNet_plus_stage1',
                                                     'PDCNetModel_model_best.pth.tar')

    # Training dataset:
    # DPED-CityScape-ADE (self-supervised data) + perturbations + sparse ground-truth on MegaDepth
    img_transforms = transforms.Compose([ArrayToTensor(get_float=False)])
    flow_transform = transforms.Compose([ArrayToTensor()])  # just put channels first and put it to float
    co_transform = None

    # geometric transformation for moving objects
    fg_tform = RandomAffine(p_flip=0.0, max_rotation=30.0,
                            max_shear=0, max_ar_factor=0.,
                            max_scale=0.3, pad_amount=0)

    min_target_area = 1300
    # object dataset
    coco_dataset_train = MSCOCO(root=settings.env.coco, split='train', version='2014',
                                min_area=min_target_area)

    # parameter for the perturbations
    perturbations_parameters_v2 = {'elastic_param': {"max_sigma": 0.04, "min_sigma": 0.1, "min_alpha": 1,
                                                     "max_alpha": 0.4},
                                   'max_sigma_mask': 10, 'min_sigma_mask': 3}

    # base training data is DPED-CityScape-ADE + perturbations
    train_dataset_, _ = PreMadeDataset(root=settings.env.training_cad_520,
                                       source_image_transform=None,
                                       target_image_transform=None,
                                       flow_transform=None,
                                       co_transform=None,
                                       split=1,
                                       get_mapping=False,
                                       add_discontinuity=True,
                                       parameters_v2=perturbations_parameters_v2,
                                       max_nbr_perturbations=15,
                                       min_nbr_perturbations=5)

    # we then adds the object on the dataset
    train_dataset_dynamic = AugmentedImagePairsDatasetMultipleObjects(foreground_image_dataset=coco_dataset_train,
                                                                      background_image_dataset=train_dataset_,
                                                                      foreground_transform=fg_tform,
                                                                      number_of_objects=1, object_proba=0.8,
                                                                      source_image_transform=img_transforms,
                                                                      target_image_transform=img_transforms,
                                                                      flow_transform=flow_transform,
                                                                      co_transform=co_transform,
                                                                      output_flow_size=[[520, 520], [256, 256]])

    # MegaDepth data
    img_transforms = transforms.Compose([ArrayToTensor(get_float=False)])
    # define config for megadepth dataset
    megadepth_cfg = {'scene_info_path': os.path.join(settings.env.megadepth_training, 'scene_info'),
                     'train_num_per_scene': 300, 'val_num_per_scene': 25,
                     'output_image_size': [520, 520], 'pad_to_same_shape': True,
                     'output_flow_size': [[520, 520], [256, 256]]}
    training_dataset_MegaDepth = MegaDepthDataset(root=settings.env.megadepth_training, split='train',
                                                  cfg=megadepth_cfg,
                                                  source_image_transform=img_transforms,
                                                  target_image_transform=img_transforms,
                                                  flow_transform=flow_transform, co_transform=co_transform,
                                                  store_scene_info_in_memory=False)
    # put store_scene_info_in_memory to True if more than 55GB of cpu memory is available. Sampling will be faster

    # final training dataset is the combination of both. Here, we overwrite the mask of train_dataset_dynamic
    train_dataset = MixDatasets(list_of_datasets=[train_dataset_dynamic, training_dataset_MegaDepth],
                                list_overwrite_mask=[True, False], list_sparse=[False, True])

    # validation data: only megadepth sparse data
    val_dataset = MegaDepthDataset(root=settings.env.megadepth_training, cfg=megadepth_cfg, split='val',
                                   source_image_transform=img_transforms,
                                   target_image_transform=img_transforms,
                                   flow_transform=flow_transform, co_transform=co_transform,
                                   store_scene_info_in_memory=False)
    # put store_scene_info_in_memory to True if more than 55GB of cpu memory is available. Sampling will be faster

    # dataloader
    train_loader = Loader('train', train_dataset, batch_size=settings.batch_size,
                          sampler=RandomSampler(train_dataset, num_samples=30000),
                          drop_last=False, training=True, num_workers=settings.n_threads)

    val_loader = Loader('val', val_dataset, batch_size=settings.batch_size, shuffle=False,
                        epoch_interval=1.0, training=False, num_workers=settings.n_threads)

    # models
    global_gocor_arguments = {'optim_iter': 3, 'init_gauss_sigma_DIMP': 0.5, 'score_act': 'relu',
                              'bin_displacement': 0.5, 'train_label_map': False, 'steplength_reg': 0.1,
                              'apply_query_loss': True, 'reg_kernel_size': 3, 'reg_inter_dim': 16,
                              'reg_output_dim': 16}
    local_gocor_arguments = {'optim_iter': 3, 'init_gauss_sigma_DIMP': 1.0, 'score_act': 'relu',
                             'bin_displacement': 0.5, 'search_size': 9, 'steplength_reg': 0.1}
    model = PDCNet_vgg16(global_gocor_arguments=global_gocor_arguments, global_corr_type='GlobalGOCor',
                         normalize='leakyrelu', cyclic_consistency=False, local_corr_type='LocalGOCor',
                         same_local_corr_at_all_levels=True, local_gocor_arguments=local_gocor_arguments,
                         local_decoder_type='OpticalFlowEstimatorResidualConnection',
                         global_decoder_type='CMDTopResidualConnection',
                         refinement_at_finest_level=True, apply_refinement_finest_resolution=True,
                         corr_for_corr_uncertainty_decoder='corr', var_1_minus_plus=1.0, var_2_minus=2.0,
                         var_2_plus_256=256 ** 2, var_2_plus=520 ** 2, estimate_three_modes=False,
                         give_layer_before_flow_to_uncertainty_decoder=True,
                         train_features=True, make_two_feature_copies=False)

    if settings.initial_pretrained_model:
        # VGG_2 is not in the pre trained mdeol weight
        pretrained_dict = torch.load(settings.initial_pretrained_model)['state_dict']
        partial_load(pretrained_dict, model)
    print(colored('==> ', 'blue') + 'model created.')

    # Optimizer
    list_pyramid_names = []
    for i in list(model.pyramid.named_parameters()):
        list_pyramid_names.append('pyramid.' + i[0])

    # filter parameters that are not in pyramid
    base_params = []
    for kv in model.named_parameters():
        if kv[0] not in list_pyramid_names:
            base_params.append(kv[1])  # save the parameter

    optimizer = \
        optim.Adam([{'params': base_params},
                    {'params': model.pyramid.parameters(), 'lr': settings.lr_feature_backbone}],
                   lr=settings.lr,
                   weight_decay=0.0004)

    # Wrap the network for multi GPU training
    if settings.multi_gpu:
        model = MultiGPU(model)

    batch_preprocessing = GLUNetBatchPreprocessing(settings, apply_mask=True, apply_mask_zero_borders=False,
                                                   sparse_ground_truth=True)

    # Loss module
    objective = NLLMixtureLaplace()
    weights_level_loss = [0.08, 0.08, 0.02, 0.02]
    # here because sparse ground-truth, we do not downsample the gt flow for the loss but upsample the
    # estimated flow instead. Weights of the loss are also adjusted accordingly so that the loss of each pyramid
    # level weight more or less the same
    loss_module_256 = MultiScaleMixtureDensity(level_weights=weights_level_loss[:2], loss_function=objective,
                                               downsample_gt_flow=False)
    loss_module = MultiScaleMixtureDensity(level_weights=weights_level_loss[2:], loss_function=objective,
                                           downsample_gt_flow=False)
    glunet_actor = GLUNetBasedActor(model, objective=loss_module, objective_256=loss_module_256,
                                    batch_processing=batch_preprocessing)

    # Scheduler
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=settings.scheduler_steps, gamma=0.5)

    # Trainer
    trainer = MatchingTrainer(glunet_actor, [train_loader, val_loader], optimizer, settings, lr_scheduler=scheduler,
                              make_initial_validation=True)

    trainer.train(settings.n_epochs, load_latest=True, fail_safe=True)




