from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import pytesseract
import re
import numpy as np
from psycopg2 import sql
import hashlib
import config
import os
import cv2
import time

# Configure Tesseract for Arabic/English
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

# def compute_piexls_hash(image):
#     """Compute a 10-character hash from the image's pixel data."""
#     if not isinstance(image, Image.Image):
#         raise TypeError("Expected PIL.Image object")
#     return hashlib.sha1(image.tobytes()).hexdigest()

def detect_image_mode(image):
    """Detect if image is dark or light mode"""
    gray = image.convert('L')
    arr = np.array(gray)
    avg_brightness = arr.mean()
    return 'dark' if avg_brightness < 128 else 'light'


def adaptive_preprocess2(image, config=None):
    """
    Enhanced image preprocessing pipeline for OCR optimization
    
    Args:
        image: PIL Image - Input image to process
        config: dict - Processing parameters (optional)
        
    Returns:
        tuple: (Processed PIL Image, detected mode)
    """
    # Configuration with default parameters
    default_config = {
        'denoise_h': 12,                # Noise reduction strength
        'scale_factor': 4,              # Quality scaling multiplier
        'adaptive_blocksize': 31,       # Size of thresholding neighborhood
        'sharp_radius': 2,              # Unsharp mask radius
        'sharp_percent': 200,           # Sharpening strength
        'max_dimension': 4000,         # Maximum allowed image dimension
        'contrast_factor': 1.5,         # Contrast enhancement
        'median_blur': False           # Enable median filtering
    }
    
    # Merge user config with defaults
    config = config or {}
    cfg = {**default_config, **config}

    # 1. Mode detection and conversion
    mode = detect_image_mode(image)
    gray = image.convert('L')
    arr = np.array(gray)

    # 2. Advanced noise reduction
    denoised = cv2.fastNlMeansDenoising(
        arr, None,
        h=cfg['denoise_h'],
        templateWindowSize=7,
        searchWindowSize=21
    )

    # Optional median blur for salt-and-pepper noise
    if cfg['median_blur']:
        denoised = cv2.medianBlur(denoised, 3)

    # 3. Adaptive thresholding with mode handling
    if mode != 'dark':
        denoised = 255 - denoised

    thresh = cv2.adaptiveThreshold(
        denoised, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        cfg['adaptive_blocksize'],
        5
    )

    # 4. Intelligent scaling with aspect ratio preservation
    processed = Image.fromarray(thresh)
    orig_width, orig_height = processed.size
    scale = calculate_scale_factor(orig_width, orig_height, cfg)
    
    processed = processed.resize(
        (int(orig_width * scale), int(orig_height * scale)),
        resample=Image.Resampling.LANCZOS
    )

    # 5. Post-processing enhancements
    processed = processed.filter(ImageFilter.UnsharpMask(
        radius=cfg['sharp_radius'],
        percent=cfg['sharp_percent'],
        threshold=3
    ))
    
    enhancer = ImageEnhance.Contrast(processed)
    processed = enhancer.enhance(cfg['contrast_factor'])

    return processed, mode

def calculate_scale_factor(width, height, cfg):
    """Dynamically calculate safe scaling factor"""
    max_dim = max(width, height)
    scale = cfg['scale_factor']
    
    # Prevent excessive upscaling for large images
    if max_dim * scale > cfg['max_dimension']:
        scale = cfg['max_dimension'] / max_dim
        
    return min(scale, cfg['scale_factor'])



def adaptive_preprocess(image):
    """Enhanced image processing pipeline with advanced preprocessing"""
    mode = detect_image_mode(image)
    gray = image.convert('L')
    
    # Convert to numpy array for OpenCV processing
    arr = np.array(gray)
    
    # Advanced noise reduction
    denoised_arr = cv2.fastNlMeansDenoising(
        arr,
        None,
        h=10,
        templateWindowSize=7,
        searchWindowSize=21
    )
    
    # Optional: Add median blur for strong noise (uncomment if needed)
    # denoised_arr = cv2.medianBlur(denoised_arr, 3)
    
    # Adaptive thresholding with mode-specific parameters
    if mode == 'dark':
        inverted_arr = 255 - denoised_arr
        thresh = cv2.adaptiveThreshold(
            inverted_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    else:
        thresh = cv2.adaptiveThreshold(
            denoised_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    
    # Convert back to PIL Image
    processed = Image.fromarray(thresh)
    
    # Quality scaling for OCR optimization
    processed = processed.resize(
        (processed.width * 5, processed.height * 5),
        resample=Image.Resampling.LANCZOS
    )
    
    # Edge-preserving sharpening
    processed = processed.filter(ImageFilter.UnsharpMask(
        radius=2,
        percent=150,
        threshold=3
    ))
    
    return processed, mode

def check_text_in_image(image_path, chosen_words) -> bool:
    print(f"Target phrases: {chosen_words}")
    start_time = time.time()
    
    subscription_variants = {
        "subscribed", "subsorived", "subscrived", "subscríved",
        "subsoribed", "subscrined", "subscroined", "subscribd",
        "subscríbed", "subscroíbed", "subscroíned", "suhscribed",
        "تم الاشتراك", "الاشترك", "الاشتراك", "الاشتواك", "الاشتواق", "الاشتوق",
        "الاشترق", "الاشتوك", "الاشتراق", "تم الأشتراك", "الأشترك", "الأشتراك",
        "الأشتواك", "الأشتواق", "الأشتوق", "الأشترق", "الأشتوك", "الأشتراق",
        "الأشراق", "الأشراك", "الاشراق", "الاشراك","الاشعراق", "الاشعراك",
        "الاشعرق", "الاشعرك","الاختراك", "الإشتراك", "الإشتراق", "الاقتراك",
        "الأقتراك", "الأقتراق", "الاشجزاك", "الافتراك", "الاقتراك", "ةكارتشالا",
        "تم", "ثم", "كم", "قم", "فم", "بم", "عم", "ته"
    }
    # "تم", "ثم", "كم", "قم", "فم", "بم", "عم", "ته"

    # Generate word variations
    words = chosen_words.strip().split()
    chosen_words_all = set()
    for word in words:
        variations = [
            word,
            word + ".com",
            "@" + word,
            word.lower(),
            ("@" + word).lower(),
            re.sub(r'(TV|قناة)$', '', word, flags=re.IGNORECASE)
        ]
        chosen_words_all.update(variations)
    
    target_chosen = {str(word).strip().lower() for word in chosen_words_all}
    target_subscription = {variant.lower() for variant in subscription_variants}
    print(f"target_chosen: {target_chosen}")
    print(f"target_subscription: {target_subscription}")


    # # Create output directory
    # os.makedirs('images', exist_ok=True)
    # base_name = os.path.splitext(os.path.basename(image_path))[0]



    # First set: 3 overlapping slices (10% height each, 5% overlap)
    slice_coords = [
        (0.1, 0.2 + i*0.05, 0.9, 0.3 + i*0.05) 
        for i in range(3)
    ]

    # Second set: 5 overlapping slices (10% height each, 5% overlap) 
    slice_coords2 = [
        (0.1, 0.1 + i*0.05, 0.9, 0.2 + i*0.05)
        for i in range(7)
    ]


    with Image.open(image_path) as img:
        width, height = img.size
        slices = []
        slices2 = []
        
        # Create and save all slices
        for i, coords in enumerate(slice_coords):
            box = (
                int(width * coords[0]),
                int(height * coords[1]),
                int(width * coords[2]),
                int(height * coords[3])
            )
            slice_img = img.crop(box)
            slices.append(slice_img)

        # Create and save all slices
        for i2, coords2 in enumerate(slice_coords2):
            box2 = (
                int(width * coords2[0]),
                int(height * coords2[1]),
                int(width * coords2[2]),
                int(height * coords2[3])
            )
            slice_img2 = img.crop(box2)
            slices2.append(slice_img2)
            
            
            # # Save original slice
            # slice_path = os.path.join('images', f'{base_name}_slice_{i}_original.png')
            # slice_img.save(slice_path)
            # slices.append(slice_img)



        # Chosen words check pipeline
        chosen_preprocessors = [
            adaptive_preprocess,
            adaptive_preprocess2,
            adaptive_preprocess_again,
            adaptive_preprocess_again2,
            # adaptive_preprocess_again3,
            # adaptive_preprocess_again4,
            # adaptive_preprocess_again5,
            # adaptive_preprocess_again6,
        ]
        found_chosen = False
        
        # Check chosen words with different preprocessing
        for preprocessor in chosen_preprocessors:
            if found_chosen: break
            
            for i, slice_img in enumerate(slices):
                # Process slice
                processed, _ = preprocessor(slice_img)

                # # Process slice and save processed version
                # processed_path = os.path.join(
                #     'images', 
                #     f'{base_name}_slice_{i}_{preprocessor.__name__}.png'
                # )
                # processed.save(processed_path)


                # OCR processing
                extracted_text = pytesseract.image_to_string(
                    processed,
                    lang='ara+eng',
                    config='--psm 11 --oem 1'
                )
                
                # Clean and tokenize
                cleaned_text = re.sub(r'[\u200e\u200f\u202a-\u202e]', '', extracted_text)
                cleaned_text = re.sub(r'[ـ_]', '', cleaned_text).lower()
                tokens = re.findall(r'[\w@.]+', cleaned_text)
                print(f"names: {tokens}")
                # Check for matches
                if any(target in token for token in tokens for target in target_chosen):
                    print(f"Found chosen words in slice {i} with {preprocessor.__name__}")
                    found_chosen = True
                    break
                
        if found_chosen:
            # Subscription check pipeline
            sub_preprocessors = [
                adaptive_preprocess,
                adaptive_preprocess2,
                adaptive_preprocess_again,
                adaptive_preprocess_again2,
                adaptive_preprocess_again3,
                adaptive_preprocess_again4,
                adaptive_preprocess_again5,
                adaptive_preprocess_again6,
            ]
            found_sub = False
            
            # Check subscription variants with different preprocessing
            for preprocessor in sub_preprocessors:
                if found_sub: break
                
                for i, slice_img in enumerate(slices2):
                    # Process slice
                    processed, _ = preprocessor(slice_img)

                    # # Process slice and save processed version
                    # processed_path = os.path.join(
                    #     'images', 
                    #     f'{base_name}_slice_{i}_{preprocessor.__name__}.png'
                    # )

                    # OCR processing
                    extracted_text = pytesseract.image_to_string(
                        processed,
                        lang='ara+eng',
                        config='--psm 11 --oem 1'
                    )
                    
                    # Clean and tokenize
                    cleaned_text = re.sub(r'[\u200e\u200f\u202a-\u202e]', '', extracted_text)
                    cleaned_text = re.sub(r'[ـ_]', '', cleaned_text).lower()
                    tokens = re.findall(r'[\w@.]+', cleaned_text)
                    print(f"subs: {tokens}")

                    # Check for matches
                    if any(variant in token for token in tokens for variant in target_subscription):
                        print(f"Found subscription in slice {i} with {preprocessor.__name__}")
                        found_sub = True
                        break
            elapsed = time.time() - start_time
            print(f"check_text_in_image1 took {elapsed:.2f} seconds")
            return found_chosen and found_sub
        
        else:
            print("No chosen words found after all preprocessing stages")
            # Initialize found_sub at the start
            found_sub = False  # Add this before any processing
            
            elapsed = time.time() - start_time
            print(f"check_text_in_image2 took {elapsed:.2f} seconds")
            
            # Then in return statement:
            return found_chosen and found_sub
        

        
    
    
    
    
def adaptive_preprocess_again(image):
    """Enhanced image processing pipeline with advanced preprocessing"""
    mode = detect_image_mode(image)
    gray = image.convert('L')
    
    # Convert to numpy array for OpenCV processing
    arr = np.array(gray)
    
    # Advanced noise reduction
    denoised_arr = cv2.fastNlMeansDenoising(
        arr,
        None,
        h=10,
        templateWindowSize=7,
        searchWindowSize=21
    )
    
    # Optional: Add median blur for strong noise (uncomment if needed)
    # denoised_arr = cv2.medianBlur(denoised_arr, 3)
    
    # Adaptive thresholding with mode-specific parameters
    if mode == 'dark':
        inverted_arr = 255 - denoised_arr
        thresh = cv2.adaptiveThreshold(
            inverted_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    else:
        thresh = cv2.adaptiveThreshold(
            denoised_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    
    # Convert back to PIL Image
    processed = Image.fromarray(thresh)
    
    # Quality scaling for OCR optimization
    processed = processed.resize(
        (processed.width * 5, processed.height * 5),
        resample=Image.Resampling.LANCZOS
    )
    
    # Edge-preserving sharpening
    processed = processed.filter(ImageFilter.UnsharpMask(
        radius=2,
        percent=150,
        threshold=3
    ))
    
    return processed, mode



def adaptive_preprocess_again2(image):
    """Enhanced image processing pipeline with advanced preprocessing"""
    mode = detect_image_mode(image)
    gray = image.convert('L')
    
    # Convert to numpy array for OpenCV processing
    arr = np.array(gray)
    
    # Advanced noise reduction
    denoised_arr = cv2.fastNlMeansDenoising(
        arr,
        None,
        h=10,
        templateWindowSize=7,
        searchWindowSize=21
    )
    
    # Optional: Add median blur for strong noise (uncomment if needed)
    # denoised_arr = cv2.medianBlur(denoised_arr, 3)
    
    # Adaptive thresholding with mode-specific parameters
    if mode == 'dark':
        inverted_arr = 255 - denoised_arr
        thresh = cv2.adaptiveThreshold(
            inverted_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    else:
        thresh = cv2.adaptiveThreshold(
            denoised_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    
    # Convert back to PIL Image
    processed = Image.fromarray(thresh)
    
    # Quality scaling for OCR optimization
    processed = processed.resize(
        (processed.width * 10, processed.height * 10),
        resample=Image.Resampling.LANCZOS
    )
    
    # Edge-preserving sharpening
    processed = processed.filter(ImageFilter.UnsharpMask(
        radius=2,
        percent=150,
        threshold=3
    ))
    
    return processed, mode


    
def adaptive_preprocess_again3(image):
    """Enhanced image processing pipeline with advanced preprocessing"""
    mode = detect_image_mode(image)
    gray = image.convert('L')
    
    # Convert to numpy array for OpenCV processing
    arr = np.array(gray)
    
    # Advanced noise reduction
    denoised_arr = cv2.fastNlMeansDenoising(
        arr,
        None,
        h=10,
        templateWindowSize=7,
        searchWindowSize=21
    )
    
    # Optional: Add median blur for strong noise (uncomment if needed)
    # denoised_arr = cv2.medianBlur(denoised_arr, 3)
    
    # Adaptive thresholding with mode-specific parameters
    if mode == 'dark':
        inverted_arr = 255 - denoised_arr
        thresh = cv2.adaptiveThreshold(
            inverted_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    else:
        thresh = cv2.adaptiveThreshold(
            denoised_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    
    # Convert back to PIL Image
    processed = Image.fromarray(thresh)
    
    # Quality scaling for OCR optimization
    processed = processed.resize(
        (processed.width * 10, processed.height * 10),
        resample=Image.Resampling.LANCZOS
    )
    
    # Edge-preserving sharpening
    processed = processed.filter(ImageFilter.UnsharpMask(
        radius=2,
        percent=150,
        threshold=3
    ))
    
    return processed, mode





def adaptive_preprocess_again4(image):
    """Enhanced image processing pipeline with advanced preprocessing"""
    mode = detect_image_mode(image)
    gray = image.convert('L')
    
    # Convert to numpy array for OpenCV processing
    arr = np.array(gray)
    
    # Advanced noise reduction
    denoised_arr = cv2.fastNlMeansDenoising(
        arr,
        None,
        h=10,
        templateWindowSize=7,
        searchWindowSize=21
    )
    
    # Optional: Add median blur for strong noise (uncomment if needed)
    # denoised_arr = cv2.medianBlur(denoised_arr, 3)
    
    # Adaptive thresholding with mode-specific parameters
    if mode == 'dark':
        inverted_arr = 255 - denoised_arr
        thresh = cv2.adaptiveThreshold(
            inverted_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    else:
        thresh = cv2.adaptiveThreshold(
            denoised_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    
    # Convert back to PIL Image
    processed = Image.fromarray(thresh)
    
    # Quality scaling for OCR optimization
    processed = processed.resize(
        (processed.width * 15, processed.height * 15),
        resample=Image.Resampling.LANCZOS
    )
    
    # Edge-preserving sharpening
    processed = processed.filter(ImageFilter.UnsharpMask(
        radius=2,
        percent=150,
        threshold=3
    ))
    
    return processed, mode






def adaptive_preprocess_again5(image):
    """Enhanced image processing pipeline with advanced preprocessing"""
    mode = detect_image_mode(image)
    gray = image.convert('L')
    
    # Convert to numpy array for OpenCV processing
    arr = np.array(gray)
    
    # Advanced noise reduction
    denoised_arr = cv2.fastNlMeansDenoising(
        arr,
        None,
        h=10,
        templateWindowSize=7,
        searchWindowSize=21
    )
    
    # Optional: Add median blur for strong noise (uncomment if needed)
    # denoised_arr = cv2.medianBlur(denoised_arr, 3)
    
    # Adaptive thresholding with mode-specific parameters
    if mode == 'dark':
        inverted_arr = 255 - denoised_arr
        thresh = cv2.adaptiveThreshold(
            inverted_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    else:
        thresh = cv2.adaptiveThreshold(
            denoised_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    
    # Convert back to PIL Image
    processed = Image.fromarray(thresh)
    
    # Quality scaling for OCR optimization
    processed = processed.resize(
        (processed.width * 15, processed.height * 15),
        resample=Image.Resampling.LANCZOS
    )
    
    # Edge-preserving sharpening
    processed = processed.filter(ImageFilter.UnsharpMask(
        radius=2,
        percent=150,
        threshold=3
    ))
    
    return processed, mode





def adaptive_preprocess_again6(image):
    """Enhanced image processing pipeline with advanced preprocessing"""
    mode = detect_image_mode(image)
    gray = image.convert('L')
    
    # Convert to numpy array for OpenCV processing
    arr = np.array(gray)
    
    # Advanced noise reduction
    denoised_arr = cv2.fastNlMeansDenoising(
        arr,
        None,
        h=10,
        templateWindowSize=7,
        searchWindowSize=21
    )
    
    # Optional: Add median blur for strong noise (uncomment if needed)
    # denoised_arr = cv2.medianBlur(denoised_arr, 3)
    
    # Adaptive thresholding with mode-specific parameters
    if mode == 'dark':
        inverted_arr = 255 - denoised_arr
        thresh = cv2.adaptiveThreshold(
            inverted_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    else:
        thresh = cv2.adaptiveThreshold(
            denoised_arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            21, 5
        )
    
    # Convert back to PIL Image
    processed = Image.fromarray(thresh)
    
    # Quality scaling for OCR optimization
    processed = processed.resize(
        (processed.width * 20, processed.height * 20),
        resample=Image.Resampling.LANCZOS
    )
    
    # Edge-preserving sharpening
    processed = processed.filter(ImageFilter.UnsharpMask(
        radius=2,
        percent=150,
        threshold=3
    ))
    
    return processed, mode