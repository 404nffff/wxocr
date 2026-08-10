import wcocr
import os
import uuid
import base64
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)
wcocr.init("./wx/opt/wechat/wxocr", "./wx/opt/wechat")

DEFAULT_SLICE_HEIGHT = 3000
DEFAULT_SLICE_OVERLAP = 200
MAX_SLICE_HEIGHT = 8000


def get_temp_dir():
    temp_dir = "temp"
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def decode_image(image_data):
    image_type, base64_data = extract_image_type(image_data)
    try:
        image_bytes = base64.b64decode(base64_data, validate=True)
    except ValueError as error:
        raise ValueError("Invalid base64 image data") from error

    try:
        image = Image.open(BytesIO(image_bytes))
        return ImageOps.exif_transpose(image), image_type
    except UnidentifiedImageError as error:
        raise ValueError("Invalid image data") from error


def overlap_ratio(first, second):
    left = max(first["left"], second["left"])
    top = max(first["top"], second["top"])
    right = min(first["right"], second["right"])
    bottom = min(first["bottom"], second["bottom"])
    intersection = max(0, right - left) * max(0, bottom - top)
    if not intersection:
        return 0

    first_area = (first["right"] - first["left"]) * (
        first["bottom"] - first["top"]
    )
    second_area = (second["right"] - second["left"]) * (
        second["bottom"] - second["top"]
    )
    return intersection / (first_area + second_area - intersection)


def merge_ocr_results(results):
    merged = []
    for result in sorted(results, key=lambda item: (item["top"], item["left"])):
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(merged)
                if existing["text"] == result["text"]
                and overlap_ratio(existing, result) >= 0.5
            ),
            None,
        )
        if duplicate_index is None:
            merged.append(result)
        elif result.get("rate", 0) > merged[duplicate_index].get("rate", 0):
            merged[duplicate_index] = result
    return sorted(merged, key=lambda item: (item["top"], item["left"]))


def recognize_long_image(image, slice_height, slice_overlap):
    width, height = image.size
    step = slice_height - slice_overlap
    temp_dir = get_temp_dir()
    results = []
    slice_count = 0

    for top in range(0, height, step):
        bottom = min(top + slice_height, height)
        slice_filename = os.path.join(temp_dir, f"{uuid.uuid4()}.png")
        try:
            image.crop((0, top, width, bottom)).convert("RGB").save(slice_filename)
            slice_result = wcocr.ocr(slice_filename)
        finally:
            if os.path.exists(slice_filename):
                os.remove(slice_filename)

        if slice_result.get("errcode") != 0:
            raise RuntimeError(
                f"OCR failed for image slice {slice_count}: {slice_result.get('errcode')}"
            )

        for item in slice_result.get("ocr_response", []):
            adjusted_item = dict(item)
            adjusted_item["top"] += top
            adjusted_item["bottom"] += top
            results.append(adjusted_item)

        slice_count += 1
        if bottom == height:
            break

    merged_results = merge_ocr_results(results)
    return {
        "errcode": 0,
        "width": width,
        "height": height,
        "slice_count": slice_count,
        "full_text": "\n".join(item["text"] for item in merged_results),
        "ocr_response": merged_results,
    }


@app.route("/ocr", methods=["POST"])
def ocr():
    try:
        # Get base64 image from request
        image_data = request.json.get("image")
        if not image_data:
            return jsonify({"error": "No image data provided"}), 400
        # Extract image type from base64 data
        image_type, base64_data = extract_image_type(image_data)
        if not image_type:
            return jsonify({"error": "Invalid base64 image data"}), 400

        # Create temp directory if not exists
        temp_dir = get_temp_dir()

        # Generate unique filename and save image
        filename = os.path.join(temp_dir, f"{str(uuid.uuid4())}.{image_type}")
        try:
            image_bytes = base64.b64decode(base64_data)
            with open(filename, "wb") as f:
                f.write(image_bytes)

            # Process image with OCR
            result = wcocr.ocr(filename)
            return jsonify({"result": result})

        finally:
            # Clean up temp file
            if os.path.exists(filename):
                os.remove(filename)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/ocr/long", methods=["POST"])
def long_ocr():
    try:
        payload = request.get_json(silent=True) or {}
        image_data = payload.get("image")
        if not image_data:
            return jsonify({"error": "No image data provided"}), 400

        slice_height = payload.get("slice_height", DEFAULT_SLICE_HEIGHT)
        slice_overlap = payload.get("slice_overlap", DEFAULT_SLICE_OVERLAP)
        if (
            not isinstance(slice_height, int)
            or not isinstance(slice_overlap, int)
            or slice_height <= 0
            or slice_height > MAX_SLICE_HEIGHT
            or slice_overlap < 0
            or slice_overlap >= slice_height
        ):
            return jsonify({"error": "Invalid slice_height or slice_overlap"}), 400

        image, _ = decode_image(image_data)
        result = recognize_long_image(image, slice_height, slice_overlap)
        return jsonify({"result": result})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        return jsonify({"error": str(error)}), 500


# 创建静态文件夹
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)


def extract_image_type(base64_data):
    # Check if the base64 data has the expected prefix
    if base64_data.startswith("data:image/"):
        # Extract the image type from the prefix
        prefix_end = base64_data.find(";base64,")
        if prefix_end != -1:
            return (
                base64_data[len("data:image/") : prefix_end],
                base64_data.split(";base64,")[-1],
            )
    return "png", base64_data


@app.route("/")
def index():
    return render_template("index.html")


# Handle unsupported methods for /ocr route
@app.route("/ocr", methods=["GET", "PUT", "DELETE", "PATCH"])
def unsupported_method():
    return jsonify({"error": "Method not allowed"}), 405


# Handle non-existent paths
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Resource not found"}), 404


if __name__ == "__main__":
    # 确保templates目录存在
    templates_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "templates"
    )
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)

    # 确保temp目录存在
    temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp")
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)

    app.run(host="0.0.0.0", port=5000, threaded=True)
