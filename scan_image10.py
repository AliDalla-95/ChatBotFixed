from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import pytesseract
import re
import numpy as np
import psycopg2
from psycopg2 import sql
import hashlib
import config
import os
import cv2

# Configure Tesseract for Arabic/English
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

def compute_piexls_hash(image):
    """Compute a 10-character hash from the image's pixel data."""
    if not isinstance(image, Image.Image):
        raise TypeError("Expected PIL.Image object")
    return hashlib.sha1(image.tobytes()).hexdigest()

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
    
    # Image regions definition
    # STATUS_BAR_COORDINATES = (0.0, 0.0, 0.5, 0.05)
    MAIN_CONTENT_COORDINATES = (0.1, 0.1, 0.9, 0.35)
    MAIN_CONTENT2_COORDINATES = (0.1, 0.2, 0.9, 0.5)

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
    
    print(f"Generated variations: {chosen_words_all}")
    
    target_chosen = {str(word).strip().lower() for word in chosen_words_all}
    target_subscription = {variant.lower() for variant in subscription_variants}

    with Image.open(image_path) as img:
        width, height = img.size

        # # Process status bar for hashing
        # status_bar_box = (
        #     int(width * STATUS_BAR_COORDINATES[0]),
        #     int(height * STATUS_BAR_COORDINATES[1]),
        #     int(width * STATUS_BAR_COORDINATES[2]),
        #     int(height * STATUS_BAR_COORDINATES[3])
        # )
        # status_bar = img.crop(status_bar_box)
        # processed_status_bar, _ = adaptive_preprocess(status_bar)
        # status_bar_hash = compute_piexls_hash(processed_status_bar)

        # Process main content region
        main_content_box = (
            int(width * MAIN_CONTENT_COORDINATES[0]),
            int(height * MAIN_CONTENT_COORDINATES[1]),
            int(width * MAIN_CONTENT_COORDINATES[2]),
            int(height * MAIN_CONTENT_COORDINATES[3])
        )
        main_content = img.crop(main_content_box)
        processed_main_content, _ = adaptive_preprocess(main_content)

        main_content2_box = (
            int(width * MAIN_CONTENT2_COORDINATES[0]),
            int(height * MAIN_CONTENT2_COORDINATES[1]),
            int(width * MAIN_CONTENT2_COORDINATES[2]),
            int(height * MAIN_CONTENT2_COORDINATES[3])
        )
        main_content2 = img.crop(main_content2_box)
        processed_main_content2, _ = adaptive_preprocess2(main_content2)

        # Save processed images for debugging
        os.makedirs('images', exist_ok=True)
        base_name = os.path.splitext(os.path.basename(image_path))[0]
        
        
        main_content2_path = os.path.join('images', f'{base_name}_main2.png')
        main_content2.save(main_content2_path)
        
        
        main_content_path = os.path.join('images', f'{base_name}_main.png')
        main_content.save(main_content_path)

        # Database operations
        # conn = psycopg2.connect(config.DATABASE_URL)
        # cur = conn.cursor()

        # cur.execute(
        #     "SELECT piexls FROM image_checks WHERE chosen_words = %s",
        #     (chosen_words,)
        # )
        # rows = cur.fetchall()
        # if rows:
        #     # Hash comparison for duplicates
        #     for row in rows:
        #         if row[0] == status_bar_hash:
        #             print("Duplicate image detected. Returning False.")
        #             cur.close()
        #             conn.close()
        #             return False

        # OCR processing
        extracted_text = pytesseract.image_to_string(
            processed_main_content,
            lang='ara+eng',
            config='--psm 11 --oem 1'
        )
         
        def clean_text(text):
            text = re.sub(r'[\u200e\u200f\u202a-\u202e]', '', text)
            text = re.sub(r'[ـ_]', '', text)
            text = re.sub(r'(\w+)[\.\*](\w+)', r'\1\2', text)
            return text.lower().strip()
        
        cleaned_text = clean_text(extracted_text)
        tokens = re.findall(r'[\w@.]+', cleaned_text)

        # cleaned_text2 = clean_text(extracted_text2)
        # tokens2 = re.findall(r'[\w@.]+', cleaned_text2)


        # Text validation
        has_words = any(
            target in token
            for token in tokens
            for target in target_chosen
        )        
        result_name = has_words
        

        if not result_name:
            print(f"1-op_name\n")
            processed_main_content, _ = adaptive_preprocess_again(main_content)
            
            # OCR processing
            extracted_text = pytesseract.image_to_string(
                processed_main_content,
                lang='ara+eng',
                config='--psm 11 --oem 1'
            )

            
            cleaned_text = clean_text(extracted_text)
            tokens = re.findall(r'[\w@.]+', cleaned_text)
            
            # Text validation
            has_words = any(
                target in token
                for token in tokens
                for target in target_chosen
            )
            result_name = has_words
        
        if not result_name:
            print(f"2-op_name\n")
            processed_main_content, _ = adaptive_preprocess_again3(main_content)
            
            # OCR processing
            extracted_text = pytesseract.image_to_string(
                processed_main_content,
                lang='ara+eng',
                config='--psm 11 --oem 1'
            )
            
            cleaned_text = clean_text(extracted_text)
            tokens = re.findall(r'[\w@.]+', cleaned_text)

            # Text validation
            has_words = any(
                target in token
                for token in tokens
                for target in target_chosen
            )
            result_name = has_words


        # if not result_name:
        #     print(f"3-op_name\n")
        #     processed_main_content, _ = adaptive_preprocess_again4(main_content)
            
        #     # OCR processing
        #     extracted_text = pytesseract.image_to_string(
        #         processed_main_content,
        #         lang='ara+eng',
        #         config='--psm 11 --oem 1'
        #     )
            
        #     cleaned_text = clean_text(extracted_text)
        #     tokens = re.findall(r'[\w@.]+', cleaned_text)
            
        #     # Text validation
        #     has_words = any(
        #         target in token
        #         for token in tokens
        #         for target in target_chosen
        #     )
        #     result_name = has_words


        # if not result_name:
        #     print(f"4-op_name\n")
        #     processed_main_content, _ = adaptive_preprocess_again5(main_content)
            
        #     # OCR processing
        #     extracted_text = pytesseract.image_to_string(
        #         processed_main_content,
        #         lang='ara+eng',
        #         config='--psm 11 --oem 1'
        #     )

            
        #     cleaned_text = clean_text(extracted_text)
        #     tokens = re.findall(r'[\w@.]+', cleaned_text)

        #     # Text validation
        #     has_words = any(
        #         target in token
        #         for token in tokens
        #         for target in target_chosen
        #     )

        #     result_name = has_words


        # if not result_name:
        #     print(f"5-op_name\n")
        #     processed_main_content, _ = adaptive_preprocess_again6(main_content)
            
        #     # OCR processing
        #     extracted_text = pytesseract.image_to_string(
        #         processed_main_content,
        #         lang='ara+eng',
        #         config='--psm 11 --oem 1'
        #     )

            
        #     cleaned_text = clean_text(extracted_text)
        #     tokens = re.findall(r'[\w@.]+', cleaned_text)

        #     # Text validation
        #     has_words = any(
        #         target in token
        #         for token in tokens
        #         for target in target_chosen
        #     )
        #     result_name = has_words 

        # else:
        #     result = has_words and has_subscription


        if result_name:
            processed_main_content2, _ = adaptive_preprocess_again2(main_content2)
            extracted_text2 = pytesseract.image_to_string(
                processed_main_content2,
                lang='ara+eng',
                config='--psm 11 --oem 1'
            )
            
            cleaned_text2 = clean_text(extracted_text2)
            tokens2 = re.findall(r'[\w@.]+', cleaned_text2)

            has_subscription = any(
                variant in token
                for token in tokens2
                for variant in target_subscription
            )
            result_sub = has_subscription
            if not result_sub:
                print(f"1-op_sub\n")
                processed_main_content, _ = adaptive_preprocess_again(main_content)
                
                # OCR processing
                extracted_text = pytesseract.image_to_string(
                    processed_main_content,
                    lang='ara+eng',
                    config='--psm 11 --oem 1'
                )

                
                cleaned_text = clean_text(extracted_text)
                tokens = re.findall(r'[\w@.]+', cleaned_text)
                
                # Text validation
                has_subscription = any(
                    variant in token
                    for token in tokens2
                    for variant in target_subscription
                )
                result_sub = has_subscription
            
            if not result_sub:
                print(f"2-op_sub\n")
                processed_main_content2, _ = adaptive_preprocess_again2(main_content2)
                
                # OCR processing
                extracted_text = pytesseract.image_to_string(
                    processed_main_content,
                    lang='ara+eng',
                    config='--psm 11 --oem 1'
                )
                
                cleaned_text = clean_text(extracted_text)
                tokens = re.findall(r'[\w@.]+', cleaned_text)

                # Text validation
                has_subscription = any(
                    variant in token
                    for token in tokens2
                    for variant in target_subscription
                )
                result_sub = has_subscription


            if not result_sub:
                print(f"3-op_sub\n")
                processed_main_content2, _ = adaptive_preprocess_again2(main_content2)
                
                # OCR processing
                extracted_text = pytesseract.image_to_string(
                    processed_main_content,
                    lang='ara+eng',
                    config='--psm 11 --oem 1'
                )
                
                cleaned_text = clean_text(extracted_text)
                tokens = re.findall(r'[\w@.]+', cleaned_text)
                
                # Text validation
                has_subscription = any(
                    variant in token
                    for token in tokens2
                    for variant in target_subscription
                )
                result_sub = has_subscription


            if not result_sub:
                print(f"4-op_sub\n")
                processed_main_content2, _ = adaptive_preprocess_again2(main_content2)
                
                # OCR processing
                extracted_text = pytesseract.image_to_string(
                    processed_main_content,
                    lang='ara+eng',
                    config='--psm 11 --oem 1'
                )

                
                cleaned_text = clean_text(extracted_text)
                tokens = re.findall(r'[\w@.]+', cleaned_text)

                # Text validation
                has_subscription = any(
                    variant in token
                    for token in tokens2
                    for variant in target_subscription
                )
                result_sub = has_subscription


            if not result_sub:
                print(f"5-op_sub\n")
                processed_main_content2, _ = adaptive_preprocess_again2(main_content2)
                
                # OCR processing
                extracted_text = pytesseract.image_to_string(
                    processed_main_content,
                    lang='ara+eng',
                    config='--psm 11 --oem 1'
                )

                
                cleaned_text = clean_text(extracted_text)
                tokens = re.findall(r'[\w@.]+', cleaned_text)

                # Text validation
                has_subscription = any(
                    variant in token
                    for token in tokens2
                    for variant in target_subscription
                )
                result_sub = has_subscription 

        result = has_words and has_subscription
        # Store successful matches
        # if result:        
        #     query = sql.SQL("""
        #         INSERT INTO image_checks (chosen_words, piexls)
        #         VALUES (%s, %s)
        #     """)
        #     cur.execute(query, (chosen_words, status_bar_hash))
        #     conn.commit()

        # cur.close()
        # conn.close()

        return result
        
    
    
    
    
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



def adaptive_preprocess_again33(image):
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



def adaptive_preprocess_again44(image):
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



def adaptive_preprocess_again55(image):
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
        (processed.width * 25, processed.height * 25),
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



def adaptive_preprocess_again66(image):
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
        (processed.width * 25, processed.height * 25),
        resample=Image.Resampling.LANCZOS
    )
    
    # Edge-preserving sharpening
    processed = processed.filter(ImageFilter.UnsharpMask(
        radius=2,
        percent=150,
        threshold=3
    ))
    
    return processed, mode