import numpy as np
from PIL import Image
import pytesseract
import spacy
import re

# Load spaCy model
nlp = spacy.load("en_core_web_sm")

def preprocess_image(image):
    """Enhance image for better OCR results"""
    gray = image.convert('L')
    threshold = gray.point(lambda x: 0 if x < 180 else 255, '1')
    return threshold

def check_text_in_image(image_path, chosen_words) -> bool:
    # Define subscription variants (lowercase)
    chosen_words1 = chosen_words
    chosen_words2 = chosen_words + ".com"
    chosen_words3 = "@" + chosen_words
    chosen_words4 = chosen_words + "HI"
    chosen_words5 = chosen_words.strip().lower()
    chosen_words6 = chosen_words3.strip().lower()
    chosen_words7 = re.sub(r'\s*TV$', '', chosen_words, flags=re.IGNORECASE)
    print(f"{chosen_words}")
    chosen_words_all = {chosen_words1,chosen_words2,chosen_words3,chosen_words4,chosen_words5,chosen_words6,chosen_words7}
    
    
    print(f"chosen_words_all{chosen_words_all}")
    subscription_variants = {"subscribed", "subscrived", "subsoribed","subscrined", "subscríbed", "subscribd","subscríbed", "subscrïbed", "subscríbd"}
    roi_coordinates = (0.0, 0.1, 0.8, 0.5)
    with Image.open(image_path) as img:
        # Calculate ROI coordinates
        width, height = img.size
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
            lang='eng',
            config='--psm 6 --oem 3'
        )

        # Process text with spaCy
        doc = nlp(extracted_text.lower())
        
        # Extract all tokens (words)
        tokens = [token.text for token in doc]
        print(f"tokens{tokens}")
        # Prepare target words (lowercase)
        target_chosen = {word.strip().lower() for word in chosen_words_all}
        target_subscription = subscription_variants
        print(f"target_chosen{target_chosen}")
        # Check for matches
        has_words = any(token in target_chosen for token in tokens)
        has_subscription = any(token in target_subscription for token in tokens)

        return has_words and has_subscription