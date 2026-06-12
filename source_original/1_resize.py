from PIL import Image
import sys

# Название вашей картинки
input_path = "input.png" 
output_path = "bot_cover_640x360.png"

try:
    with Image.open(input_path) as img:
        # Принудительное изменение размера до 640x360
        # Используем LANCZOS для сохранения высокого качества при уменьшении
        resized_img = img.resize((640, 360), Image.Resampling.LANCZOS)
        
        resized_img.save(output_path, quality=95)
        print(f"Готово! Файл сохранен как: {output_path}")
        print("Теперь отправляйте этот файл в BotFather.")
except FileNotFoundError:
    print(f"Ошибка: Файл {input_path} не найден.")
except Exception as e:
    print(f"Произошла ошибка: {e}")