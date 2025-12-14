import requests
import time
import logging
import os
import re

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class FreeOCRProcessor:
    def __init__(self):
        self.base_url = "https://api.ocr.space/parse/image"
        self.public_key = "helloworld"  # Public demo key
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

    def process_image(self, image_path):
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
                            "language": "eng",
                            "isOverlayRequired": False,
                            "OCREngine": 2
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

def check_text_in_image(image_path, chosen_words):
    """
    Processes an image with OCR and checks if the chosen words are present.
    :param image_path: Path to the image file.
    :param chosen_words: A string (for one word) or a list of words to check.
    :return: True if all chosen words are found in the OCR text, False otherwise.
    """
    ocr_processor = FreeOCRProcessor()
    ocr_text = ocr_processor.process_image(image_path)
    
    if not ocr_text:
        return False

    if isinstance(chosen_words, str):
        chosen_words = [chosen_words]

    for word in chosen_words:
        pattern = r'\b' + re.escape(word) + r'\b'
        if not re.search(pattern, ocr_text, re.IGNORECASE):
            return False
    return True

if __name__ == "__main__":
    test_image = "ali.jpg"  # Change this to your test image path
    # Check for multiple words, e.g., "example" and "test"
    result = check_text_in_image(test_image, ["example", "test"])
    print(f"OCR Check Result: {result}")
