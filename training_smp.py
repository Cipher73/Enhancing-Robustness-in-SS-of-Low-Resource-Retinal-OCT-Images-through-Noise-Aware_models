import os
import glob
import cv2
import numpy as np
from sklearn.preprocessing import LabelEncoder
from sklearn.utils import shuffle
from sklearn.metrics import accuracy_score, confusion_matrix
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
from torchvision import transforms
from torchvision.datasets import ImageFolder
from torch.utils.data import DataLoader, random_split
import wandb
import segmentation_models_pytorch as smp
import segmentation_models_pytorch.encoders as encoders
from catalyst.dl import SupervisedRunner
from catalyst.dl import CriterionCallback, MetricAggregationCallback
from catalyst.loggers.wandb import WandbLogger
from catalyst.utils import get_device
import matplotlib.patches as mpatches
import torch.nn.functional as F



class SegmentationModelTrainer:
    def __init__(self, config):
        self.config = config
        self.models = {
            'Unet_resnet34': 'saved_models/Unet_resnet34_model_WN_smp.pth',
            'Unet_resnet50': 'saved_models/Unet_resnet50_model_WN_smp.pth',
            'DeepLabV3Plus_resnet34': 'saved_models/DeepLabV3Plus_resnet34_model_WN_smp.pth',
            'DeepLabV3Plus_resnet50': 'saved_models/DeepLabV3Plus_resnet50_model_WN_smp.pth'
        }

    def build_segmentation_model(self,achitecture,encoder, n_classes, activation):
        if achitecture == 'DeepLabV3Plus':
            model = smp.DeepLabV3Plus(
                encoder_name=encoder,
                classes=n_classes,
                activation=activation
            )
        else:
            model = smp.Unet(encoder_name=encoder, classes=n_classes, activation=activation)
        return model


    def train_model(self, model, dataloaders):
        optimizer = optim.Adam(model.parameters(), lr=self.config['learning_rate'])
        criterion = nn.CrossEntropyLoss()
        target_criterion = nn.CrossEntropyLoss()
        model.to(get_device())
        model.train()
        #metrics =[smp.utils.metrics.IoU(threshold=0.5)]
        
        for epoch in range(self.config['num_epochs']):
            for phase in ['train', 'valid']:
                if phase == 'train':
                    model.train()
                else:
                    model.eval()

                running_loss = 0.0
                running_iou = 0.0
                target_running_loss = 0.0
                source_dataloader = dataloaders['train']
                target_domain_dataloader = dataloaders['target_train']
                 

                for batch_idx, ((source_inputs, source_targets), (target_inputs, target_targets)) in enumerate(zip(source_dataloader, target_domain_dataloader)):
                    inputs, targets = source_inputs.to(get_device()), source_targets.to(get_device())
                    target_inputs = target_inputs.to(get_device())

                    optimizer.zero_grad()

                    with torch.set_grad_enabled(phase == 'train'):
                        outputs = model(inputs.float())
                        loss = criterion(outputs, targets)
                        iou = compute_iou(outputs, targets)

                        if phase == 'train':
                            loss.backward()
                            optimizer.step()
                    target_outputs = model(target_inputs.float())
                    target_loss = target_criterion(target_outputs, F.softmax(target_outputs, dim=1).argmax(dim=1))
                    
                    running_loss += loss.item() * inputs.size(0)
                    running_iou += iou.item() * inputs.size(0)
                    target_running_loss+= target_loss.item() * target_inputs.size(0)

                epoch_loss = running_loss / len(dataloaders[phase].dataset)
                epoch_iou = running_iou / len(dataloaders[phase].dataset)
                target_epoch_loss = target_running_loss / len(target_domain_dataloader.dataset)
                

                print(f'{phase} Loss: {epoch_loss:.4f}, IoU: {epoch_iou:.4f}')
                print(f'{phase} Target Loss: {target_epoch_loss:.4f}')

                # Log metrics to wandb for each epoch
                wandb.log({f'{phase}_loss': epoch_loss, f'{phase}_iou': epoch_iou})
                wandb.log({f'{phase}_target_loss': target_epoch_loss})

        return model

    def save_model(self, model, model_name):
        torch.save(model.state_dict(), f'saved_models/{model_name}_WN_smp.pth')

    def train_single_model(self, dataloaders, achitecture, encoder):
        model = self.build_segmentation_model(achitecture, encoder, self.config['n_classes'], self.config['activation'])
        trained_model = self.train_model(model, dataloaders)
        model_name = f"{achitecture}_{encoder}"
        self.save_model(trained_model, f'{model_name}_model')

    def train_segmentation_models(self, dataloaders):
        for achitecture in self.config['achitecture']:
            for encoder in self.config['encoders']:
                with wandb.init(
                    project=self.config['wandb_project'],
                    config=self.config,
                    name=f"{achitecture}_{encoder}"
                ):
                    self.train_single_model(dataloaders, achitecture, encoder)


    def evaluate_models_on_test(self, model, dataloader, model_name):
        model.eval()
        all_preds = []
        all_targets = []
        all_test_images = []
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        model.to(device)


        with torch.no_grad():
            for inputs, targets in dataloader['test']:
                inputs,targets = inputs.to(device), targets.to(device)
                all_test_images.append(inputs.cpu().numpy())
                preds = model(inputs.float())
                all_preds.append(preds.cpu().numpy())
                all_targets.append(targets.cpu().numpy())

        all_preds = np.concatenate(all_preds)
        X_test = np.concatenate(all_test_images)
        all_targets = np.concatenate(all_targets)
        y_pred_argmax = np.argmax(all_preds, axis=1)
        y_test_argmax = all_targets
        cm = confusion_matrix(y_test_argmax.flatten(), y_pred_argmax.flatten())

        # Normalize the confusion matrix
        cm_normalized = cm / cm.sum(axis=1)

        # Plot and save the confusion matrix as an image
        plt.imshow(cm_normalized, cmap='hot')
        plt.colorbar()
        plt.xlabel('Predicted labels')
        plt.ylabel('True labels')
        plt.title('Normalized Confusion Matrix')
        plt.savefig(f"plots/{model_name}_confusion_matrix_WN_smp.png")
        plt.close()

        # Log confusion matrix
        wandb.init(
            project=self.config['wandb_project'],
            config=self.config,
            name=f"accuracy_{model_name}"
        )


        class_colors = [[0, 0, 0], [0, 0, 255], [0, 255, 0], [255, 0, 0], [255, 255, 0], [255, 0, 255], [0, 255, 255], [255, 255, 255], [0, 0, 0], [128, 128, 128]]
        class_names = ['background', 'NFL', 'GCL-IPL', 'INL', 'OPL', 'ONL-ISM', 'ISM', 'OS-RPE', 'background', 'Fluid']

        for i in range(min(3, len(X_test))):
            prediction = y_pred_argmax[i]
            colored_mask = np.zeros((*prediction.shape, 3), dtype=np.uint8)
            for class_id, color in enumerate(class_colors):
                colored_mask[prediction == class_id] = color
            
            # do the same for the ground truth mask
            ground_truth = y_test_argmax[i]
            colored_ground_truth = np.zeros((*ground_truth.shape, 3), dtype=np.uint8)
            for class_id, color in enumerate(class_colors):
                colored_ground_truth[ground_truth == class_id] = color
                
            plt.title('Image')
            plt.subplot(2, 3, 1)
            plt.imshow(colored_ground_truth)
            plt.title('Ground Truth')
            plt.subplot(2, 3, 2)
            plt.imshow(colored_mask)
            plt.title('Prediction')
             # Create legend excluding classes with class_id 0 and 8
            legend_patches = [mpatches.Patch(color=np.array(color) / 255, label=class_names[class_id]) for class_id, color in enumerate(class_colors) if class_id not in [0, 8]]
            plt.legend(handles=legend_patches, bbox_to_anchor=(1.05, 1), loc='upper left', borderaxespad=0.)
            plt.savefig(f"plots/{model_name}_prediction_{i}.png")
            plt.close()
            wandb.log({f"{model_name}_prediction_{i}": wandb.Image(f"plots/{model_name}_prediction_{i}.png")})
            
        # Calculate accuracy
        accuracy = accuracy_score(y_test_argmax.flatten(), y_pred_argmax.flatten())
        wandb.run.finish()
        model.train()

        return accuracy


#function to compute IoU
def compute_iou(outputs, targets):
    intersection = (outputs.argmax(dim=1) & targets).float().sum((1, 2))
    union = (outputs.argmax(dim=1) | targets).float().sum((1, 2))
    iou = (intersection + 1e-10) / (union + 1e-10)
    return iou.mean()


class DataProcessor:
    @staticmethod
    def get_data(directory_path, flag):
        images = []
        for img_path in glob.glob(os.path.join(directory_path, "*.tif")):
            img = cv2.imread(img_path, flag)
            images.append(img)
        images = np.array(images)
        return images

    @staticmethod
    def shuffle_data(images, masks):
        images, masks = shuffle(images, masks, random_state=0)
        return images, masks

    def preprocess_data(self, train_images, train_masks, val_images, val_masks, test_images, test_masks):
        train_images = np.array(train_images)
        train_masks = np.array(train_masks)
        val_images = np.array(val_images)
        val_masks = np.array(val_masks)
        test_images = np.array(test_images)
        test_masks = np.array(test_masks)

        # Label encoding for training masks
        labelencoder = LabelEncoder()
        n, h, w = train_masks.shape
        train_masks_reshaped = train_masks.reshape(-1, 1)
        train_masks_reshaped_encoded = labelencoder.fit_transform(train_masks_reshaped)
        train_masks_encoded_original_shape = train_masks_reshaped_encoded.reshape(n, h, w)

        X_train = train_images
        y_train = train_masks_encoded_original_shape
        X_test = test_images
        y_test = test_masks
        X_val = val_images
        y_val = val_masks
        X_train = train_images.transpose(0, 3, 1, 2)  # Transpose image dimensions
        y_train = train_masks_encoded_original_shape
        X_test = test_images.transpose(0, 3, 1, 2)    # Transpose image dimensions
        y_test = test_masks
        X_val = val_images.transpose(0, 3, 1, 2)      # Transpose image dimensions
        y_val = val_masks
        n_classes = len(np.unique(y_train))
        print(f"Number of classes = {n_classes}")
        y_train_cat = torch.tensor(y_train, dtype=torch.long)
        y_test_cat = torch.tensor(y_test, dtype=torch.long)
        y_val_cat = torch.tensor(y_val, dtype=torch.long)
        print(X_train[0].shape, y_train_cat[0].shape)
        #X_train = X_train.astype(np.float64)
        return X_train, y_train_cat, X_test, y_test_cat, X_val, y_val_cat, n_classes

 

    
data_processor = DataProcessor()

# Load and preprocess data
train_images = np.array(data_processor.get_data("Dataset/train/images/", 1))
target_domain_train_images = np.array(data_processor.get_data("Dataset/train/noisy_images/", 1))
train_masks = np.array(data_processor.get_data("Dataset/train/masks/", 0))
val_images = np.array(data_processor.get_data("Dataset/val/images/", 1))
val_masks = np.array(data_processor.get_data("Dataset/val/masks/", 0))
test_images = np.array(data_processor.get_data("Dataset/test/images/", 1))
test_domain_images = np.array(data_processor.get_data("Dataset/test/noisy_images/", 1))
test_masks = np.array(data_processor.get_data("Dataset/test/masks/", 0))
test_domain_masks = np.array(data_processor.get_data("Dataset/test/noisy_masks/", 0))
# Shuffle the data
train_images, train_masks = data_processor.shuffle_data(train_images, train_masks)
val_images, val_masks = data_processor.shuffle_data(val_images, val_masks)
test_images, test_masks = data_processor.shuffle_data(test_images, test_masks)
 
#target domain data
target_domain_train_images, target_domain_train_image2 = data_processor.shuffle_data(target_domain_train_images, target_domain_train_images)
test_domain_images, test_domain_masks = data_processor.shuffle_data(test_domain_images, test_domain_masks)

# concatianate test sets 
test_images = np.concatenate((test_images, test_domain_images), axis=0)
test_masks = np.concatenate((test_masks, test_domain_masks), axis=0)


# Preprocess the data
X_train, y_train_cat, X_test, y_test_cat, X_val, y_val_cat, n_classes = data_processor.preprocess_data(
    train_images, train_masks, val_images, val_masks, test_images, test_masks
)

X_target_train, y_target_train_cat, X_target_test, y_target_test_cat, X_target_val, y_target_val_cat, n_classes = data_processor.preprocess_data(
    target_domain_train_images, train_masks, val_images, val_masks, test_domain_images, test_domain_masks)


config = {
    'wandb_project': "SSOCTLRE",
    'num_epochs': 10,
    'batch_size': 2,
    'achitecture': ['Unet', 'DeepLabV3Plus'],
    'encoders': ['resnet34', 'resnet50'],
    'n_classes': n_classes,
    'activation': 'softmax',
    'learning_rate': 0.001,
    'optimizer': "adam"
}



# Initialize and use the SegmentationModelTrainer class
trainer = SegmentationModelTrainer(config)
dataloaders = {
    "train": DataLoader(list(zip(X_train, y_train_cat)), batch_size=config['batch_size'], shuffle=True),
    "target_train": DataLoader(list(zip(X_target_train, y_target_train_cat)), batch_size=config['batch_size'], shuffle=True),
    "valid": DataLoader(list(zip(X_val, y_val_cat)), batch_size=config['batch_size'], shuffle=False),
    "test": DataLoader(list(zip(X_test, y_test_cat)), batch_size=config['batch_size'], shuffle=False)
}
#trainer.train_segmentation_models(dataloaders)

# Evaluate trained models on test data
for model_name in trainer.models:
    #load model
    arch, encoder = model_name.split('_')
    model = trainer.build_segmentation_model(arch, encoder, config['n_classes'], config['activation'])
    model.load_state_dict(torch.load(f"saved_models/{model_name}_model_WN_smp.pth"))
    
    accuracy = trainer.evaluate_models_on_test(model,dataloaders, model_name)
    print(f"{model_name} Test Accuracy: {accuracy}")

