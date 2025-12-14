from PIL import Image

def compare_images(image1_path, image2_path):
    image1 = Image.open(image1_path)
    image2 = Image.open(image2_path)
    return list(image1.getdata()) == list(image2.getdata())

print(compare_images("image1.jpg", "image2.jpg"))