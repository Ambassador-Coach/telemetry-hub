import numpy as np
from PIL import Image, ImageGrab
# Note: All image processing now handled by C++ accelerator - this file contains legacy stubs only

def apply_preprocessing(*args, **kwargs) -> np.ndarray:
    """
    Legacy preprocessing function - now handled by C++ accelerator.
    This is a stub that returns the input frame unchanged.
    
    The C++ accelerator performs all preprocessing internally including:
    - Upscaling
    - Color enhancement  
    - Grayscale conversion
    - Unsharp masking
    - Adaptive thresholding
    - Text mask cleaning
    - Symbol enhancement
    """
    # Extract the first argument (frame) if provided
    if args:
        frame = args[0]
        if isinstance(frame, np.ndarray):
            return frame
    
    # Return a minimal frame if no valid input
    return np.zeros((100, 100, 3), dtype=np.uint8)


def capture_preview_frame(
    roi_coordinates,
    return_as_array=False,
    enable_preprocessing=False,
    preprocessing_kwargs=None,
    preview_in_grayscale=False
):
    """
    Capture a screenshot for the given ROI using PIL only.
    Preprocessing is now handled by C++ accelerator, so enable_preprocessing is ignored.

    Args:
        roi_coordinates (tuple): (x1, y1, x2, y2) bounding box.
        return_as_array (bool): If True, returns a NumPy array.
                                Otherwise, returns a PIL Image.
        enable_preprocessing (bool): Ignored - C++ accelerator handles preprocessing.
        preprocessing_kwargs (dict): Ignored - C++ accelerator handles preprocessing.
        preview_in_grayscale (bool): If True, convert preview to grayscale.

    Returns:
        PIL.Image or np.ndarray: The screenshot (RGB format from PIL).
    """
    try:
        x1, y1, x2, y2 = roi_coordinates
        if x1 >= x2 or y1 >= y2:
            raise ValueError(f"Invalid ROI coordinates: {roi_coordinates}. Width and height must be positive.")

        # Capture using PIL
        screen_pil = ImageGrab.grab(bbox=(x1, y1, x2, y2))
        frame_captured_np = np.array(screen_pil)  # RGB format

        if return_as_array:
            return frame_captured_np  # Return RGB numpy array

        # Create PIL Image for preview (downscaled)
        preview_width = max(1, screen_pil.width // 2)
        preview_height = max(1, screen_pil.height // 2)
        
        preview_pil = screen_pil.resize((preview_width, preview_height), Image.Resampling.LANCZOS)
        
        if preview_in_grayscale:
            preview_pil = preview_pil.convert('L')
        
        return preview_pil

    except Exception as e:
        print(f"[ERROR] Capturing preview frame: {e}")
        error_pil_img = Image.new("RGB", (100, 50), "gray")
        if return_as_array:
            return np.array(error_pil_img)
        else:
            return error_pil_img