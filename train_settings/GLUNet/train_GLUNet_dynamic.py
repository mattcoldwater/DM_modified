from termcolor import colored
import torch.optim as optim
import torchvision.transforms as transforms
import torch.optim.lr_scheduler as lr_scheduler
from utils_data.image_transforms import ArrayToTensor
from training.actors.batch_processing import GLUNetBatchPreprocessing
from training.losses.basic_losses import EPE
from training.losses.multiscale_loss import MultiScaleFlow
from training.trainers.matching_trainer import MatchingTrainer
from utils_data.loaders import Loader
from admin.multigpu import MultiGPU
from training.actors.self_supervised_actor import GLUNetBasedActor
from models.GLUNet.GLU_Net import glunet_vgg16
from datasets.load_pre_made_datasets.load_pre_made_dataset import PreMadeDataset
from datasets.object_augmented_dataset import MSCOCO, AugmentedImagePairsDatasetMultipleObjects
from datasets.object_augmented_dataset.synthetic_object_augmentation_for_pairs_multiple_ob import RandomAffine


def run(settings):
    settings.description = 'Default train settings for GLU-Net on the dynamic dataset (from GOCor paper)'
    settings.data_mode = 'euler'
    settings.batch_size = 16
    settings.n_threads = 8
    settings.multi_gpu = True
    settings.print_interval = 500
    settings.lr = 0.0001
    settings.scheduler_steps = [100, 120, 130]
    settings.n_epochs = 150

    # 1. Define training and validation datasets
    # datasets, pre-processing of the images is done within the network function !
    img_transforms = transforms.Compose([ArrayToTensor(get_float=False)])
    flow_transform = transforms.Compose([ArrayToTensor()])  # just put channels first and put it to float
    co_transform = None

    # geometric transformation for moving objects
    fg_tform = RandomAffine(p_flip=0.0, max_rotation=30.0,
                            max_shear=0, max_ar_factor=0.,
                            max_scale=0.3, pad_amount=0)

    # object dataset
    min_target_area = 1300
    coco_dataset_train = MSCOCO(root=settings.env.coco, split='train', version='2014',
                                min_area=min_target_area)

    # base training data is DPED-CityScape-ADE + 1 object from COCO
    train_dataset, _ = PreMadeDataset(root=settings.env.training_cad_520,
                                      source_image_transform=None,
                                      target_image_transform=None,
                                      flow_transform=None,
                                      co_transform=None,
                                      split=1)  # only training

    # we then adds the object on the dataset
    train_dataset = AugmentedImagePairsDatasetMultipleObjects(foreground_image_dataset=coco_dataset_train,
                                                              background_image_dataset=train_dataset,
                                                              foreground_transform=fg_tform,
                                                              number_of_objects=1, object_proba=0.8,
                                                              source_image_transform=img_transforms,
                                                              target_image_transform=img_transforms,
                                                              flow_transform=flow_transform,
                                                              co_transform=co_transform)

    # validation dataset: DPED-CityScape-ADE + 1 object from COCO
    _, val_dataset = PreMadeDataset(root=settings.env.validation_cad_520,
                                    source_image_transform=None,
                                    target_image_transform=None,
                                    flow_transform=None,
                                    co_transform=None,
                                    split=0)

    val_dataset = AugmentedImagePairsDatasetMultipleObjects(foreground_image_dataset=coco_dataset_train,
                                                            background_image_dataset=val_dataset,
                                                            number_of_objects=1, object_proba=0.8,
                                                            foreground_transform=fg_tform,
                                                            source_image_transform=img_transforms,
                                                            target_image_transform=img_transforms,
                                                            flow_transform=flow_transform,
                                                            co_transform=co_transform)

    # 2. Define dataloaders
    train_loader = Loader('train', train_dataset, batch_size=settings.batch_size, shuffle=True,
                          drop_last=False, training=True, num_workers=settings.n_threads)

    val_loader = Loader('val', val_dataset, batch_size=settings.batch_size, shuffle=False,
                        epoch_interval=1.0, training=False, num_workers=settings.n_threads)

    # 3. Define model
    model = glunet_vgg16(global_corr_type='feature_corr_layer', normalize='relu_l2norm',
                         normalize_features=True, cyclic_consistency=True,
                         local_corr_type='feature_corr_layer', give_flow_to_refinement_module=False,
                         local_decoder_type='OpticalFlowEstimator',
                         global_decoder_type='CMDTop')
    print(colored('==> ', 'blue') + 'model created.')

    # Wrap the network for multi GPU training
    if settings.multi_gpu:
        model = MultiGPU(model)

    # 4. Define batch_processing and objective
    batch_processing = GLUNetBatchPreprocessing(settings, apply_mask=False, apply_mask_zero_borders=False,
                                                sparse_ground_truth=False)

    # 5. Define loss module
    objective = EPE()
    weights_level_loss = [0.32, 0.08, 0.02, 0.01]
    loss_module_256 = MultiScaleFlow(level_weights=weights_level_loss[:2], loss_function=objective,
                                     downsample_gt_flow=True)
    loss_module = MultiScaleFlow(level_weights=weights_level_loss[2:], loss_function=objective, downsample_gt_flow=True)

    # 6. Define actor
    GLUNetActor = GLUNetBasedActor(model, objective=loss_module, objective_256=loss_module_256,
                                   batch_processing=batch_processing)

    # 7. Define Optimizer
    optimizer = \
        optim.Adam(filter(lambda p: p.requires_grad, model.parameters()),
                   lr=settings.lr,
                   weight_decay=0.0004)

    # 8. Define Scheduler
    scheduler = lr_scheduler.MultiStepLR(optimizer,
                                         milestones=settings.scheduler_steps,
                                         gamma=0.5)

    # 9. Define Trainer
    trainer = MatchingTrainer(GLUNetActor, [train_loader, val_loader], optimizer, settings, lr_scheduler=scheduler)

    trainer.train(settings.n_epochs, load_latest=True, fail_safe=True)




