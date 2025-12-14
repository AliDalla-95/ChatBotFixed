import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
import cv2
import numpy as np

# Set the correct path for Tesseract-OCR
pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"

def preprocess_image(image_path):
    """Enhances image quality before OCR to improve accuracy."""
    
    # Open image
    img = Image.open(image_path)
    
    # Convert to grayscale
    img = img.convert('L')

    # Enhance contrast
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.5)  # Stronger contrast boost

    # Convert to NumPy array for OpenCV processing
    img_cv = np.array(img)

    # Apply Bilateral Filter (Removes noise but keeps edges sharp)
    img_cv = cv2.bilateralFilter(img_cv, 9, 75, 75)

    # Apply Adaptive Thresholding (Binarization)
    img_cv = cv2.adaptiveThreshold(
        img_cv, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )

    # Sharpening Filter to enhance text edges
    kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]])
    img_cv = cv2.filter2D(img_cv, -1, kernel)

    # Convert back to PIL Image
    processed_img = Image.fromarray(img_cv)
    
    return processed_img


def preprocess1_image(image_path):
    """Enhance image quality before text recognition."""
    # Open image and convert to grayscale
    img = Image.open(image_path)
    
    # Convert to grayscale
    img = img.convert('L')
    
    # Enhance contrast
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2)  # Increase contrast
    
    # Convert to NumPy array for OpenCV processing
    img_cv = np.array(img)

    # Apply Gaussian blur to remove noise
    img_cv = cv2.GaussianBlur(img_cv, (3, 3), 0)

    # Apply adaptive thresholding (binarization)
    img_cv = cv2.adaptiveThreshold(
        img_cv, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )

    # Convert back to PIL Image
    processedd_img = Image.fromarray(img_cv)
    
    return processedd_img




def check_text_in_image(image_path, required_text):
    """Checks if the required text exists in the processed image."""
    
    # Process the image for OCR
    processed_img = preprocess_image(image_path)
    processedd_img = preprocess1_image(image_path)

    # Use Tesseract OCR to extract text
    extracted_text = pytesseract.image_to_string(processed_img, lang='eng')
    extracted_text1 = pytesseract.image_to_string(processedd_img, lang='eng')


    # Normalize text for better matching
    extracted_text = extracted_text.strip()
    extracted_text1 = extracted_text1.strip()
    required_text = required_text.lower().strip()

    print(f"Extracted Text: {extracted_text}")  # Debugging
    print(f"Extracted Text1: {extracted_text1}")  # Debugging

    # Ensure required text and subscription keywords are present
    return required_text in extracted_text or required_text in extracted_text1 and ("Subscribed" in extracted_text or "Subscribed" in extracted_text1)



