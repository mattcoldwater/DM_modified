from termcolor import colored
import torch.optim as optim
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
from utils_data.euler_wrapper import prepare_data
from admin.loading import partial_load
import torch


def run(settings):
    settings.description = 'Default train settings for PDCNet+ stage 1'
    settings.data_mode = 'euler'
    settings.batch_size = 14
    settings.n_threads = 8
    settings.multi_gpu = True
    settings.print_interval = 500
    settings.lr = 0.0001
    settings.scheduler_steps = [50, 90]
    settings.n_epochs = 150
    settings.initial_pretrained_model = None

    # dataset parameters
    # independently moving objects
    settings.nbr_objects = 4
    settings.min_area_objects = 1300
    settings.compute_object_reprojection_mask = True
    # very important, we compute the object reprojection mask, will be used for training

    # perturbations
    perturbations_parameters_v2 = {'elastic_param': {"max_sigma": 0.04, "min_sigma": 0.1, "min_alpha": 1,
                                                     "max_alpha": 0.4},
                                   'max_sigma_mask': 10, 'min_sigma_mask': 3}

    # Train dataset: synthetic data with perturbations + independently moving objects
    # object foreground dataset
    fg_tform = RandomAffine(p_flip=0.0, max_rotation=90.0,
                            max_shear=0, max_ar_factor=0.,
                            max_scale=0.3, pad_amount=0)

    prepare_data(settings.env.coco_tar, mode=settings.data_mode)
    coco_dataset_train = MSCOCO(root=settings.env.coco, split='train', version='2014',
                                min_area=settings.min_area_objects)

    # base dataset with image pairs and ground-truth flow field + adding perturbations
    prepare_data(settings.env.training_cad_520_tar, mode=settings.data_mode)
    train_dataset, _ = PreMadeDataset(root=settings.env.training_cad_520,
                                      source_image_transform=None,
                                      target_image_transform=None,
                                      flow_transform=None,
                                      co_transform=None,
                                      split=1,
                                      get_mapping=False,
                                      add_discontinuity=True,
                                      parameters_v2=perturbations_parameters_v2,
                                      max_nbr_perturbations=15,
                                      min_nbr_perturbations=5)  # only training

    # add independently moving objects + compute the reprojection mask
    # datasets, pre-processing of the images is done within the network function !
    source_img_transforms = transforms.Compose([ArrayToTensor(get_float=False)])
    target_img_transforms = transforms.Compose([ArrayToTensor(get_float=False)])
    flow_transform = transforms.Compose([ArrayToTensor()])  # just put channels first and put it to float
    co_transform = None
    train_dataset = AugmentedImagePairsDatasetMultipleObjects(
        foreground_image_dataset=coco_dataset_train, background_image_dataset=train_dataset,
        foreground_transform=fg_tform, source_image_transform=source_img_transforms,
        target_image_transform=target_img_transforms, flow_transform=flow_transform,
        co_transform=co_transform, number_of_objects=settings.nbr_objects,
        compute_object_reprojection_mask=settings.compute_object_reprojection_mask)

    # validation dataset
    prepare_data(settings.env.validation_cad_520_tar, mode=settings.data_mode)
    _, val_dataset = PreMadeDataset(root=settings.env.validation_cad_520,
                                    source_image_transform=None,
                                    target_image_transform=None,
                                    flow_transform=None,
                                    co_transform=None,
                                    split=0,
                                    add_discontinuity=True,
                                    parameters_v2=perturbations_parameters_v2,
                                    max_nbr_perturbations=15,
                                    min_nbr_perturbations=5,
                                    get_mapping=False)  # only validation

    val_dataset = AugmentedImagePairsDatasetMultipleObjects(
        foreground_image_dataset=coco_dataset_train, background_image_dataset=val_dataset,
        foreground_transform=fg_tform, source_image_transform=source_img_transforms,
        target_image_transform=target_img_transforms, flow_transform=flow_transform, co_transform=co_transform,
        number_of_objects=settings.nbr_objects,
        compute_object_reprojection_mask=settings.compute_object_reprojection_mask)

    # dataloader
    train_loader = Loader('train', train_dataset, batch_size=settings.batch_size, shuffle=True,
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
                         give_layer_before_flow_to_uncertainty_decoder=True)

    if settings.initial_pretrained_model:
        # VGG_2 is not in the pre trained mdeol weight
        pretrained_dict = torch.load(settings.initial_pretrained_model)
        if 'state_dict' in pretrained_dict:
            pretrained_dict = pretrained_dict['state_dict']
        partial_load(pretrained_dict, model)
        print('Initialised weights')
    print(colored('==> ', 'blue') + 'model created.')

    # Wrap the network for multi GPU training
    if settings.multi_gpu:
        model = MultiGPU(model)

    batch_processing = GLUNetBatchPreprocessing(settings, apply_mask=True, apply_mask_zero_borders=False,
                                                sparse_ground_truth=False)

    # Loss module
    objective = NLLMixtureLaplace()
    weights_level_loss = [0.32, 0.08, 0.02, 0.01]
    loss_module_256 = MultiScaleMixtureDensity(level_weights=weights_level_loss[:2], loss_function=objective,
                                               downsample_gt_flow=True)
    loss_module = MultiScaleMixtureDensity(level_weights=weights_level_loss[2:], loss_function=objective,
                                           downsample_gt_flow=True)
    glunet_actor = GLUNetBasedActor(model, objective=loss_module, objective_256=loss_module_256,
                                    batch_processing=batch_processing)

    # Optimizer
    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=settings.lr, weight_decay=0.0004)

    # Scheduler
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=settings.scheduler_steps, gamma=0.5)

    trainer = MatchingTrainer(glunet_actor, [train_loader, val_loader], optimizer, settings, lr_scheduler=scheduler,
                              make_initial_validation=False)

    trainer.train(settings.n_epochs, load_latest=True, fail_safe=True)




