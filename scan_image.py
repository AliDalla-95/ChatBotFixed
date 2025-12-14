import os
import time
import logging
import re
from PIL import Image
import pytesseract
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class OCRHandler(FileSystemEventHandler):
    VALID_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp')
    
    def __init__(self):
        os.makedirs('outputs', exist_ok=True)
        
    def on_created(self, event):
        if not event.is_directory:
            file_path = event.src_path
            if file_path.lower().endswith(self.VALID_EXTENSIONS):
                logger.info("Processing new file: %s", file_path)
                result = check_text_in_image(file_path, ["Subscribed"])
                if result:
                    logger.info("Valid subscription found in: %s", file_path)
                else:
                    logger.warning("No valid subscription found in: %s", file_path)

def preprocess_image(image):
    """Enhance image for better OCR results"""
    # Convert to grayscale
    gray = image.convert('L')
    
    # Apply thresholding
    threshold = gray.point(lambda x: 0 if x < 180 else 255, '1')
    
    return threshold

def check_text_in_image(image_path,chosen_words,roi_coordinates: tuple = (0.0, 0.1, 0.8, 0.5)) -> bool:
    
    """
    Processes image and checks for specified words
    
    :param image_path: Path to the image file
    :param chosen_words: List of words to search for
    :param roi_coordinates: Region of interest (left, top, right, bottom) ratios
    :return: True if all words are found, False otherwise
    """
    
    
    print(f"{image_path} and {chosen_words}")

    try:
        # Validate inputs
        if not isinstance(chosen_words, (list, str)):
            raise ValueError("chosen_words must be list or string")
            
        # Convert to list if single string
        if isinstance(chosen_words, str):
            chosen_words = [chosen_words]

        with Image.open(image_path) as img:
            width, height = img.size
            
            # Calculate ROI coordinates
            left = int(width * roi_coordinates[0])
            top = int(height * roi_coordinates[1])
            right = int(width * roi_coordinates[2])
            bottom = int(height * roi_coordinates[3])
            
            # Crop and process image
            cropped = img.crop((left, top, right, bottom))
            processed = preprocess_image(cropped)
            
            # OCR processing
            extracted_text = pytesseract.image_to_string(
                processed,
                lang='eng',  # Multiple languages
                config='--psm 6 --oem 3'
            )
            
            logger.debug(f"OCR results for {image_path}:\n{extracted_text}")
            print(f"{extracted_text}")

            # Build regex patterns
            keywords_pattern = '|'.join(map(re.escape, chosen_words))
            word_pattern = re.compile(
                rf'\b({keywords_pattern})\b',
                flags=re.IGNORECASE
            )
            
            subscription_pattern = re.compile(
                r'\b(subscribed|subscrived|subsoribed|subscrined)\b',
                flags=re.IGNORECASE
            )

            # Check matches
            has_words = word_pattern.search(extracted_text) is not None
            has_subscription = subscription_pattern.search(extracted_text) is not None
            
            return has_words and has_subscription

    except FileNotFoundError:
        logger.error(f"Image not found: {image_path}")
        return False
    except Image.UnidentifiedImageError:
        logger.error(f"Invalid image format: {image_path}")
        return False
    except Exception as e:
        logger.error(f"Error processing {image_path}: {str(e)}", exc_info=True)
        return False

def start_monitoring(path_to_watch):
    observer = Observer()
    event_handler = OCRHandler()
    observer.schedule(event_handler, path_to_watch, recursive=False)
    observer.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    # Example usage
    test_image = "test.jpg"
    chosen_words = ["Sony Pictures Releasing UK"]
    
    # # تشغيل الفحص
    # result = check_text_in_image(test_image, chosen_words)
    
    # # عرض النتيجة
    # if result:
    #     print("✅ النص المطلوب موجود في الصورة!")
    #     # نقل الصورة في حالة النجاح
    #     os.rename(test_image, f"processed_images/success_{os.path.basename(test_image)}")
    # else:
    #     print("❌ النص المطلوب غير موجود")
    #     # نقل الصورة في حالة الفشل
    #     os.rename(test_image, f"processed_images/failed_{os.path.basename(test_image)}")
    
    # # Start monitoring folder
    # start_monitoring("./watch_folder")