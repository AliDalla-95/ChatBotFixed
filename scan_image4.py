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
    print(f"chosen_words: {chosen_words}")
    
    # Define subscription variants (lowercase)
    subscription_variants = {"subscribed", "subsorived","subscrived", "subscríved", "subsoribed", "subscrined", "subscroined", "subscribd", "subscríbed", "subscroíbed", "subscroíned"}
    roi_coordinates = (0.0, 0.1, 0.8, 0.5)

    # Extract the first word from chosen_words (before the first space)
    # first_word = chosen_words.strip().split(" ")[0]  # Get the first word
    words = chosen_words.strip().split(" ")  # Get the first word
    chosen_words_all = set()
    # print(f"words: {words}")
    variations = []
    for word in words:
        # Original and common variations
        variations.extend([
            word,
            word + ".com",
            "@" + word,
            word.lower(),
            ("@" + word).lower(),
        ])
        # TV-removed variations
        variations.append(word)
    
    chosen_words_all.update(variations)
    # Generate variations for the first word
    # chosen_words_all = set()
    # variations = [
    #     first_word,  # Original
    #     first_word + ".com",
    #     "@" + first_word,
    #     first_word.lower(),
    #     ("@" + first_word).lower(),
    #     sec_word,
    #     sec_word + ".com",
    #     "@" + sec_word,
    #     sec_word.lower(),
    #     ("@" + sec_word).lower(),
    #     re.sub(r'\s*TV$', '', first_word, flags=re.IGNORECASE),
    #     re.sub(r'\s*TV$', '', sec_word, flags=re.IGNORECASE)
    # ]
    # chosen_words_all.update(variations)

    
    print(f"variations: {chosen_words_all}")
    
    # Normalize all targets to lowercase
    target_chosen = {str(word).strip().lower() for word in chosen_words_all}
    target_subscription = {variant.lower() for variant in subscription_variants}

    print(f"target_chosen: {target_chosen}")
    print(f"target_subscription: {target_subscription}")

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
        tokens = [token.text for token in doc]

        # Check for SUBSTRING matches (not exact)
        has_words = any(
            target in token 
            for token in tokens 
            for target in target_chosen
        )
        print(f"tokens: {tokens}")
        print(f"target_chosen: {target_chosen}")

        has_subscription = any(
            variant in token 
            for token in tokens 
            for variant in target_subscription
        )

        return has_words and has_subscription