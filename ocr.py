import pytesseract
from PIL import Image, ImageFilter, ImageOps
import logging
import os
import re
import concurrent.futures
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import pdf2image  # For PDF support

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def preprocess_image(image):
    """
    Preprocess the image to improve OCR accuracy.
    Converts the image to grayscale and applies thresholding.
    """
    # Convert to grayscale
    gray_image = image.convert('L')
    # Optionally, apply a median filter to reduce noise
    filtered_image = gray_image.filter(ImageFilter.MedianFilter())
    # Apply thresholding to get a binary image; adjust threshold as needed.
    threshold_value = 140
    binary_image = filtered_image.point(lambda x: 0 if x < threshold_value else 255, '1')
    return binary_image

class OfflineOCRProcessor:
    def __init__(self):
        self.supported_langs = {
            "eng": "eng",
        }
        
        # Configure Tesseract path if needed (uncomment if required)
        # pytesseract.pytesseract.tesseract_cmd = r'<path_to_your_tesseract_executable>'
        
        # Verify Tesseract installation
        try:
            pytesseract.get_tesseract_version()
        except pytesseract.TesseractNotFoundError:
            logger.error("Tesseract OCR not found. Please install it from https://github.com/tesseract-ocr/tesseract")
            raise

    def process_image(self, image_path, language="eng"):
        try:
            # Handle PDF files
            if image_path.lower().endswith('.pdf'):
                images = pdf2image.convert_from_path(image_path)
                text = ""
                for image in images:
                    # Preprocess each image page
                    processed_image = preprocess_image(image)
                    text += pytesseract.image_to_string(processed_image, lang=language, config="--psm 6")
                return text
            
            # Handle image files
            with Image.open(image_path) as img:
                processed_img = preprocess_image(img)
                return pytesseract.image_to_string(processed_img, lang=language, config="--psm 6")
            
        except Exception as e:
            logger.error(f"OCR processing error: {str(e)}")
            return None

class OCRHandler(FileSystemEventHandler):
    VALID_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp', '.pdf')

    def __init__(self, processor):
        self.processor = processor
        os.makedirs('outputs', exist_ok=True)

    def on_created(self, event):
        if not event.is_directory:
            file_path = event.src_path
            if file_path.lower().endswith(self.VALID_EXTENSIONS):
                logger.info("Processing new file: %s", file_path)
                text = self.processor.process_image(file_path)
                if text:
                    output_file = f"outputs/{os.path.basename(file_path)}_result.txt"
                    with open(output_file, 'w') as f:
                        f.write(text)
                    logger.info("Saved results to: %s", output_file)
                else:
                    logger.warning("OCR failed for: %s", file_path)

def check_text_in_image(image_path, chosen_words):
    """
    Processes an image with OCR and checks if the chosen words and an exact subscription confirmation are present.
    Returns True only if:
      - The OCR text is non-empty.
      - All chosen words are found as complete words.
      - The text contains the exact word "Subscribed" (case insensitive),
        and not variations like "Subscribe" or "Subscribers".
    Otherwise, returns False.
    """
    ocr_processor = OfflineOCRProcessor()
    ocr_text = ""
    
    # Try OCR in supported languages (currently only English)
    for lang in ["eng"]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(ocr_processor.process_image, image_path, lang)
            try:
                text = future.result(timeout=12)
            except concurrent.futures.TimeoutError:
                logger.warning("OCR timed out after 12 seconds for language %s", lang)
                continue
            except Exception as e:
                logger.error("OCR processing error for language %s: %s", lang, e)
                continue
                
            if text:
                ocr_text += " " + text
                
    print(f"OCR text: {ocr_text}")
    if not ocr_text:
        return False

    # (Optional) Extract words containing numbers or standalone numbers
    words_with_numbers = re.findall(
        r'\w*\d+\w*',
        ocr_text,
        flags=re.IGNORECASE | re.UNICODE
    )
    print(f"Words with numbers: {words_with_numbers}")

    if isinstance(chosen_words, str):
        chosen_words = [chosen_words]

    # Check that each chosen word exists in the OCR text
    for word in chosen_words:
        pattern = r'\b' + re.escape(word) + r'\b'
        if not re.search(pattern, ocr_text, re.IGNORECASE | re.UNICODE):
            return False

    # Check for exact subscription confirmation
    subscription_pattern = r'\b(Subscribed)\b'
    match = re.search(subscription_pattern, ocr_text, re.IGNORECASE | re.UNICODE)
    if not match:
        logger.warning("No subscription confirmation found in OCR text.")
        return False
    if match.group(1).lower() != 'subscribed':
        logger.warning("Subscription confirmation does not exactly match.")
        return False

    return True
