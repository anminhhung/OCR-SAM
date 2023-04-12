import cv2
import gradio as gr
import numpy as np
import PIL.Image as Image
import torch
from matplotlib import pyplot as plt
# MMOCR
from mmocr.apis.inferencers import MMOCRInferencer
from mmocr.utils import poly2bbox
# SAM
from segment_anything import SamPredictor, sam_model_registry

# Diffusers
from diffusers import StableDiffusionControlNetPipeline, ControlNetModel

det_config = 'mmocr_dev/configs/textdet/dbnetpp/dbnetpp_swinv2_base_w16_in21k.py'  # noqa
det_weight = 'mmocr_dev/checkpoints/db_swin_mix_pretrain.pth'
rec_config = 'mmocr_dev/configs/textrecog/unirec/unirec.py'
rec_weight = 'mmocr_dev/checkpoints/unirec.pth'
sam_checkpoint = 'segment-anything-main/checkpoints/sam_vit_h_4b8939.pth'
device = 'cuda'
sam_type = 'vit_h'

# BUILD MMOCR
mmocr_inferencer = MMOCRInferencer(
    det_config, det_weight, rec_config, rec_weight, device=device)
# Build SAM
sam = sam_model_registry[sam_type](checkpoint=sam_checkpoint)
sam_predictor = SamPredictor(sam)

# Build Diffusers
controlnet = ControlNetModel.from_pretrained(
    "lllyasviel/sd-controlnet-seg", torch_dtype=torch.float16)
pipe = StableDiffusionControlNetPipeline.from_pretrained(
    "runwayml/stable-diffusion-v1-5",
    controlnet=controlnet,
    torch_dtype=torch.float16)
pipe = pipe.to("cuda")


def show_mask(mask, ax, random_color=False):
    if random_color:
        color = np.concatenate([np.random.random(3), np.array([0.6])], axis=0)
    else:
        color = np.array([30 / 255, 144 / 255, 255 / 255, 0.6])
    h, w = mask.shape[-2:]
    mask_image = mask.reshape(h, w, 1) * color.reshape(1, 1, -1)
    ax.imshow(mask_image)


def run_mmocr_sam(img: np.ndarray, ):
    """Run MMOCR and SAM

    Args:
        img (np.ndarray): Input image
        det_config (str): Path to the config file of the selected detection
            model.
        det_weight (str): Path to the custom checkpoint file of the selected
            detection model.
        rec_config (str): Path to the config file of the selected recognition
            model.
        rec_weight (str): Path to the custom checkpoint file of the selected
            recognition model.
        sam_checkpoint (str): Path to the custom checkpoint file of the
            selected SAM model.
        sam_type (str): Type of the selected SAM model. Defaults to 'vit_h'.
        device (str): Device used for inference. Defaults to 'cuda'.
    """
    # Build MMOCR

    result = mmocr_inferencer(img)['predictions'][0]
    rec_texts = result['rec_texts']
    det_polygons = result['det_polygons']
    det_bboxes = torch.tensor([poly2bbox(poly) for poly in det_polygons],
                              device=sam_predictor.device)
    transformed_boxes = sam_predictor.transform.apply_boxes_torch(
        det_bboxes, img.shape[:2])
    # SAM inference
    sam_predictor.set_image(img, image_format='BGR')
    masks, _, _ = sam_predictor.predict_torch(
        point_coords=None,
        point_labels=None,
        boxes=transformed_boxes,
        multimask_output=False,
    )
    # Draw results
    plt.figure()
    # close axis
    plt.axis('off')
    # convert img to RGB
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    plt.imshow(img)
    outputs = {}
    output_str = ''
    for idx, (mask, rec_text, polygon, bbox) in enumerate(
            zip(masks, rec_texts, det_polygons, det_bboxes)):
        show_mask(mask, plt.gca(), random_color=True)
        polygon = np.array(polygon).reshape(-1, 2)
        # convert polygon to closed polygon
        polygon = np.concatenate([polygon, polygon[:1]], axis=0)
        plt.plot(polygon[:, 0], polygon[:, 1], '--', color='b', linewidth=4)
        # plot text on the left top corner of the polygon
        text_string = f'idx:{idx}, {rec_text}'
        plt.text(
            bbox[0],
            bbox[1],
            text_string,
            color='y',
            fontsize=15,
        )
        output_str += f'{idx}:{rec_text}' + '\n'
        outputs[idx] = dict(
            mask=mask.cpu().numpy().tolist(), polygon=polygon.tolist())
    plt.savefig('output.png')
    # convert plt to numpy
    img = cv2.cvtColor(
        np.array(plt.gcf().canvas.renderer._renderer), cv2.COLOR_RGB2BGR)
    plt.close()
    return img, output_str, outputs


def run_downstream(img: np.ndarray, mask_results, index: str, prompt: str):
    """Run downstream tasks

    Args:
        img (np.ndarray): Input image
        mask_results (str): Mask results from SAM
        index (str): Index of the selected text
        task (str): Downstream task selected
        prompt (str): Inpainting prompt
    """
    # Diffuser
    mask_results = eval(mask_results)
    mask = np.array(mask_results[int(index)]['mask'][0])
    # covert mask to binary mask
    mask = (mask > 0.5).astype(np.uint8)
    # convert mask to three channels
    mask = np.stack([mask, mask, mask], axis=-1)
    mask = Image.fromarray(mask)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(img)
    original_size = img.size
    generator = torch.manual_seed(0)
    diff_result = pipe(
        prompt=prompt,
        num_inference_steps=20,
        generator=generator,
        image=mask.resize((512, 512))).images[0]
    masked_diff_result = Image.fromarray(
        np.array(diff_result) * np.array(mask.resize(
            (512, 512)))).resize(original_size)
    # img + masked_diff_result
    img = Image.fromarray(np.array(img) + np.array(masked_diff_result))
    img = np.array(img)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    return img


if __name__ == '__main__':

    with gr.Blocks() as demo:
        with gr.Row():
            with gr.Column(scale=1):
                input_image = gr.Image(label='Input Image')
                sam_results = gr.Textbox(label='Detection Results')
                mask_results = gr.Textbox(label='Mask Results', max_lines=2)
                mmocr_sam = gr.Button('Run MMOCR and SAM')
                text_index = gr.Textbox(label='Select Text Index')
                prompt = gr.Textbox(label='Inpainting Prompt')
                downstream = gr.Button('Run Downstream Tasks')
            with gr.Column(scale=1):
                output_image = gr.Image(label='Output Image')
                gr.Markdown("## Image Examples")
                gr.Examples(
                    examples=[
                        'example_images/ex1.jpg', 'example_images/ex2.jpg',
                        'example_images/ex3.jpg', 'example_images/ex4.jpg'
                    ],
                    inputs=input_image,
                )
            mmocr_sam.click(
                fn=run_mmocr_sam,
                inputs=[input_image],
                outputs=[output_image, sam_results, mask_results])
            downstream.click(
                fn=run_downstream,
                inputs=[input_image, mask_results, text_index, prompt],
                outputs=[output_image])

    demo.launch(debug=True)