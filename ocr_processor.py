import requests
import time
import logging
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import re
import concurrent.futures
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class FreeOCRProcessor:
    def __init__(self):
        self.base_url = "https://api.ocr.space/parse/image"
        self.public_key = "K85958377488957"  # Public demo key
        self.max_retries = 3
        self.retry_delay = 5  # seconds
        self.hourly_limit = 300
        self.request_count = 0
        self.last_reset = time.time()

    def _reset_counter(self):
        current_time = time.time()
        if current_time - self.last_reset > 3600:
            self.request_count = 0
            self.last_reset = current_time

    def process_image(self, image_path, language="eng"):
        self._reset_counter()
        
        if self.request_count >= self.hourly_limit:
            logger.warning("Hourly rate limit reached")
            return None

        for attempt in range(self.max_retries):
            try:
                with open(image_path, 'rb') as f:
                    response = requests.post(
                        self.base_url,
                        files={"file": f},
                        data={
                            "apikey": self.public_key,
                            "language": language,
                            "isOverlayRequired": False,
                            "OCREngine": 1
                        },
                        timeout=10
                    )

                self.request_count += 1

                if response.status_code == 200:
                    result = response.json()
                    if 'ParsedResults' in result:
                        return result['ParsedResults'][0]['ParsedText']
                    else:
                        logger.error("API error: %s", result.get('ErrorMessage', 'Unknown error'))
                        return None

                elif response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    logger.warning("Rate limited. Retrying after %s seconds", retry_after)
                    time.sleep(retry_after)
                    continue

                else:
                    logger.error("API error: HTTP %s", response.status_code)
                    return None

            except requests.exceptions.RequestException as e:
                logger.error("Request failed: %s", str(e))
                time.sleep(self.retry_delay * (attempt + 1))
                continue

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

    # ===== TEMPORARY TEST CODE =====
    # Uncomment these 3 lines to test a single image

def check_text_in_image(image_path, chosen_words):
    """
    Processes an image with OCR and checks if the chosen words are present.
    
    :param image_path: Path to the image file.
    :param chosen_words: A string (for one word) or a list of words to check.
    :return: True if all chosen words are found in the OCR text, False otherwise.
    """
    ocr_processor = FreeOCRProcessor()
    ocr_text = ""
    
    # # before add counter for online or offline
    # for lang in ["eng", "ara", "rus"]:  # Check all three languages
    #     text = ocr_processor.process_image(image_path, lang)
    #     if text:
    #         ocr_text += " " + text  # Combine results from all languages

    #After add counter for online or offline
    # Try OCR in several languages
    for lang in ["eng", "ara", "rus"]:
                                #ProcessPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(ocr_processor.process_image, image_path, lang)
            try:
                text = future.result(timeout=12)  # Wait for up to 12 seconds
            except concurrent.futures.TimeoutError:
                logger.warning("OCR timed out after 12 seconds for language %s", lang)
                return False
            # except Exception as e:
            #     logger.error("OCR processing error for language %s: %s", lang, e)
            #     return False
        if text:
            ocr_text += " " + text  # Combine results from all languages

    if not ocr_text:
        return False
    # Extract words containing numbers or standalone numbers
    # words_with_numbers = re.findall(
    #     r'\w*\d+\w*',  # Matches words with numbers or standalone numbers
    #     ocr_text,
    #     flags=re.IGNORECASE | re.UNICODE
    # )
    # logger.info(f"Words with numbers: {words_with_numbers}")
    if isinstance(chosen_words, str):
        chosen_words = [chosen_words]

    for word in chosen_words:
        pattern = r'\b' + re.escape(word) + r'\b'
        # print(f"word:{word}")
        if not re.search(pattern, ocr_text, re.IGNORECASE | re.UNICODE):
            return False #, words_with_numbers
        
        # Check if "Subscribed" is in the OCR text
    if not re.search(r'\bSubscribed\b|\bتم الاشتراك\b|\bВы подписаны\b', ocr_text, re.IGNORECASE | re.UNICODE):
        return False #, words_with_numbers

    return True #, words_with_numbers