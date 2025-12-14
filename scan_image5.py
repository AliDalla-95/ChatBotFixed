from PIL import Image
import pytesseract
import spacy
import re
import xx_sent_ud_sm
# Load spaCy models for both languages
# nlp_en = spacy.load("en_core_web_sm")
# nlp_ar = spacy.load("ar_core_web_sm")
nlp = spacy.load("xx_sent_ud_sm")
nlp = xx_sent_ud_sm.load()

def preprocess_image(image):
    """Enhance image for better OCR results"""
    gray = image.convert('L')
    threshold = gray.point(lambda x: 0 if x < 180 else 255, '1')
    return threshold

def check_text_in_image(image_path, chosen_words) -> bool:
    print(f"chosen_words: {chosen_words}")
    
    # Define subscription variants (include Arabic)
    subscription_variants = {
        # English variants
        "subscribed", "subsorived", "subscrived", "subscríved",
        "subsoribed", "subscrined", "subscroined", "subscribd",
        "subscríbed", "subscroíbed", "subscroíned",
        # Arabic variants
        "مشترك", "مشتركون", "مشترک", "مشاریك", "تم الاشتراك", "مشترکین"
    }
    
    roi_coordinates = (0.0, 0.1, 0.8, 0.5)

    # Generate word variations
    words = chosen_words.strip().split(" ")
    chosen_words_all = set()
    
    for word in words:
        variations = [
            word,
            word + ".com",
            "@" + word,
            word.lower(),
            ("@" + word).lower(),
            re.sub(r'\s*TV$', '', word, flags=re.IGNORECASE)
        ]
        chosen_words_all.update(variations)
    
    print(f"variations: {chosen_words_all}")
    
    # Normalize targets
    target_chosen = {str(word).strip().lower() for word in chosen_words_all}
    target_subscription = {variant.lower() for variant in subscription_variants}

    with Image.open(image_path) as img:
        # Crop and process image
        width, height = img.size
        left = int(width * roi_coordinates[0])
        top = int(height * roi_coordinates[1])
        right = int(width * roi_coordinates[2])
        bottom = int(height * roi_coordinates[3])

        cropped = img.crop((left, top, right, bottom))
        processed = preprocess_image(cropped)

        # OCR with multi-language support
        extracted_text = pytesseract.image_to_string(
            processed,
            lang='eng+ara',  # Added Arabic support
            config='--psm 6 --oem 3'
        ).lower()

        # Process text with both NLP models
        doc_en = nlp_en(extracted_text)
        doc_ar = nlp_ar(extracted_text)
        
        # Combine tokens from both languages
        tokens = [token.text for token in doc_en] + [token.text for token in doc_ar]

        # Check matches
        has_words = any(
            target in token
            for token in tokens
            for target in target_chosen
        )
        
        has_subscription = any(
            variant in token
            for token in tokens
            for variant in target_subscription
        )

        return has_words and has_subscription