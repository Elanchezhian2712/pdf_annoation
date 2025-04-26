# annotator/views.py
import fitz  # PyMuPDF
import tempfile
import os
import json
from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse, Http404
from django.views.decorators.http import require_POST, require_GET
from django.urls import reverse
from .forms import PDFUploadForm

# --- Constants ---
ANNOTATION_TYPES = ['tick', 'cross', 'blue_mark']
SESSION_KEY_PDF_PATH = 'pdf_path'
SESSION_KEY_ANNOTATIONS = 'annotations'
SESSION_KEY_PAGE_INFO = 'page_info' # Store original dimensions and scale
RENDER_SCALE_FACTOR = 2.0 # Render images at 2x resolution for clarity

# --- Helper Functions ---
def get_session_data(request):
    """Safely get annotation data and counts from session."""
    annotations = request.session.get(SESSION_KEY_ANNOTATIONS, [])
    counts = {atype: sum(1 for ann in annotations if ann['type'] == atype) for atype in ANNOTATION_TYPES}
    return annotations, counts

def get_pdf_document(request):
    """Get the fitz document from the path stored in session."""
    pdf_path = request.session.get(SESSION_KEY_PDF_PATH)
    if not pdf_path or not os.path.exists(pdf_path):
        # Handle error: PDF path missing or file deleted
        # Maybe redirect to upload or show an error message
        return None
    try:
        doc = fitz.open(pdf_path)
        return doc
    except Exception as e:
        print(f"Error opening PDF: {e}")
        # Handle error appropriately
        return None

# --- Views ---
def upload_pdf(request):
    if request.method == 'POST':
        form = PDFUploadForm(request.POST, request.FILES)
        if form.is_valid():
            pdf_file = request.FILES['pdf_file']

            # Save uploaded file temporarily
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                for chunk in pdf_file.chunks():
                    temp_pdf.write(chunk)
                temp_pdf_path = temp_pdf.name

            # Store path and clear previous annotations in session
            request.session[SESSION_KEY_PDF_PATH] = temp_pdf_path
            request.session[SESSION_KEY_ANNOTATIONS] = []
            request.session[SESSION_KEY_PAGE_INFO] = [] # Reset page info

            # Pre-process to get page count and dimensions
            try:
                doc = fitz.open(temp_pdf_path)
                page_infos = []
                for i, page in enumerate(doc):
                    rect = page.rect
                    page_infos.append({
                        'page_num': i,
                        'orig_width': rect.width,
                        'orig_height': rect.height,
                        'render_scale': RENDER_SCALE_FACTOR
                    })
                request.session[SESSION_KEY_PAGE_INFO] = page_infos
                doc.close()
            except Exception as e:
                 # Handle PDF processing error (e.g., corrupted file)
                 os.unlink(temp_pdf_path) # Clean up temp file
                 form.add_error('pdf_file', f'Error processing PDF: {e}')
                 return render(request, 'annotator/upload.html', {'form': form})


            return redirect('annotate_pdf')
    else:
        form = PDFUploadForm()
        # Clean up any lingering temp file from a previous session if needed
        # (Session might expire, but file remains) - Robust cleanup is complex
        # For simplicity here, we rely on OS temp cleaning or manual deletion

    return render(request, 'annotator/upload.html', {'form': form})

@require_GET
def annotate_pdf(request):
    pdf_path = request.session.get(SESSION_KEY_PDF_PATH)
    page_info = request.session.get(SESSION_KEY_PAGE_INFO)

    if not pdf_path or not page_info:
        return redirect('upload_pdf') # Redirect if no PDF uploaded

    annotations, counts = get_session_data(request)

    # Generate URLs for page images dynamically
    page_image_urls = [
        reverse('get_page_image', args=[info['page_num']]) for info in page_info
    ]

    context = {
        'page_count': len(page_info),
        'page_image_urls': page_image_urls,
        'page_info': page_info, # Pass dimensions/scale info if needed by JS
        'annotations': annotations, # Pass current annotations if needed for initial display
        'counts': counts,
    }
    return render(request, 'annotator/annotate.html', context)

@require_GET
def get_page_image(request, page_num):
    doc = get_pdf_document(request)
    page_info_list = request.session.get(SESSION_KEY_PAGE_INFO, [])

    if not doc or page_num >= len(doc) or page_num >= len(page_info_list):
        raise Http404("PDF page not found or PDF not loaded.")

    page_info = page_info_list[page_num]
    scale = page_info.get('render_scale', RENDER_SCALE_FACTOR)
    mat = fitz.Matrix(scale, scale) # Create zoom matrix

    try:
        page = doc[page_num]
        pix = page.get_pixmap(matrix=mat, alpha=False) # Render page
        doc.close() # Close doc ASAP

        img_data = pix.tobytes("png") # Get image data in PNG format
        return HttpResponse(img_data, content_type="image/png")

    except Exception as e:
        if doc: doc.close()
        print(f"Error generating page image {page_num}: {e}")
        raise Http404("Error generating page image.")

@require_POST
def add_annotation(request):
    pdf_path = request.session.get(SESSION_KEY_PDF_PATH)
    page_info_list = request.session.get(SESSION_KEY_PAGE_INFO)
    if not pdf_path or not page_info_list:
        return JsonResponse({'status': 'error', 'message': 'No PDF loaded'}, status=400)

    try:
        data = json.loads(request.body)
        page_num = int(data['page_num'])
        img_x = float(data['x'])  # Coordinate from click event on image
        img_y = float(data['y'])
        annotation_type = data['type']

        if annotation_type not in ANNOTATION_TYPES:
            return JsonResponse({'status': 'error', 'message': 'Invalid annotation type'}, status=400)

        if page_num < 0 or page_num >= len(page_info_list):
            return JsonResponse({'status': 'error', 'message': 'Invalid page number'}, status=400)

        # --- Coordinate Conversion ---
        page_info = page_info_list[page_num]
        scale = page_info['render_scale']
        pdf_x = img_x / scale
        pdf_y = img_y / scale

        # --- Load Existing Annotations ---
        # --- Store Annotation or Remove If Exists ---
        annotations = request.session.get(SESSION_KEY_ANNOTATIONS, [])
        duplicate_found = False

        for existing in annotations:
            if (
                existing['page_num'] == page_num and
                abs(existing['pdf_x'] - pdf_x) < 1 and  # use tolerance for float comparison
                abs(existing['pdf_y'] - pdf_y) < 1 and
                existing['type'] == annotation_type
            ):
                annotations.remove(existing)
                duplicate_found = True
                break

        if not duplicate_found:
            new_annotation = {
                'page_num': page_num,
                'pdf_x': pdf_x,
                'pdf_y': pdf_y,
                'type': annotation_type,
            }
            annotations.append(new_annotation)

        request.session[SESSION_KEY_ANNOTATIONS] = annotations  # Update session

        # Recalculate counts as usual
        _, counts = get_session_data(request)

        return JsonResponse({
            'status': 'ok',
            'counts': counts,
            'action': 'removed' if duplicate_found else 'added',
            'page_num': page_num,
            'pdf_x': pdf_x,
            'pdf_y': pdf_y,
            'type': annotation_type
        })


    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    except (KeyError, ValueError) as e:
        return JsonResponse({'status': 'error', 'message': f'Missing or invalid data: {e}'}, status=400)
    except Exception as e:
        print(f"Unexpected error in add_annotation: {e}")
        return JsonResponse({'status': 'error', 'message': 'An internal error occurred'}, status=500)


@require_GET
def download_pdf(request):
    doc = get_pdf_document(request)
    annotations, counts = get_session_data(request)

    if not doc:
        # Handle error: maybe redirect to upload or show error page
        return HttpResponse("Error: PDF not found or could not be opened.", status=404)

    # --- Define paths to annotation images ---
    # Create these PNG files in your static directory
    # Ensure they have transparent backgrounds
    static_dir = os.path.join(os.path.dirname(__file__), 'static', 'annotator', 'img') # Adjust path as needed
    icon_paths = {
        'tick': os.path.join(static_dir, 'tick.png'),
        'cross': os.path.join(static_dir, 'cross.png'),
        'blue_mark': os.path.join(static_dir, 'blue_mark.png'),
    }
    icon_size = 15 # Size in PDF points (adjust as needed)

    try:
        # Add annotations to the PDF
        for ann in annotations:
            page_num = ann['page_num']
            if page_num < len(doc):
                page = doc[page_num]
                pdf_x = ann['pdf_x']
                pdf_y = ann['pdf_y']
                ann_type = ann['type']

                icon_path = icon_paths.get(ann_type)
                if icon_path and os.path.exists(icon_path):
                    # Center the icon on the click point
                    rect = fitz.Rect(
                        pdf_x - icon_size / 2,
                        pdf_y - icon_size / 2,
                        pdf_x + icon_size / 2,
                        pdf_y + icon_size / 2
                    )
                    try:
                        page.insert_image(rect, filename=icon_path, keep_proportion=True, overlay=True)
                    except Exception as insert_err:
                        print(f"Error inserting image {icon_path} on page {page_num}: {insert_err}")
                        # Optionally draw a fallback shape
                        # page.draw_circle(fitz.Point(pdf_x, pdf_y), 5, ...)
                else:
                    print(f"Warning: Icon image not found for type '{ann_type}' at {icon_path}")
                    # Optionally draw a default shape if icon missing

        # Add counts to the first page (optional)
        if len(doc) > 0:
            first_page = doc[0]
            text_y = 20 # Position from top
            text = f"Annotation Counts: ✅ Ticks: {counts.get('tick', 0)}, ❌ Crosses: {counts.get('cross', 0)}, 🔵 Blue Marks: {counts.get('blue_mark', 0)}"
            # Note: Using emoji directly might depend on font support in the PDF viewer.
            # Consider using ASCII like (T), (X), (B) or loading a font that supports symbols.
            text_rect = fitz.Rect(50, text_y, first_page.rect.width - 50, text_y + 20)
            # Use a standard font like Helvetica
            rc = first_page.insert_textbox(text_rect, text, fontsize=10, fontname="helv", align=fitz.TEXT_ALIGN_LEFT)
            if rc < 0:
                print(f"Warning: Text for counts might not have fit: {text}")


        # Save modified PDF to a byte stream
        pdf_data = doc.tobytes(garbage=4, deflate=True, clean=True)
        doc.close()

        # Clean up the original temporary file
        original_temp_path = request.session.get(SESSION_KEY_PDF_PATH)
        if original_temp_path and os.path.exists(original_temp_path):
             try:
                 os.unlink(original_temp_path)
                 # Clear session keys after successful download and cleanup
                 request.session.pop(SESSION_KEY_PDF_PATH, None)
                 request.session.pop(SESSION_KEY_ANNOTATIONS, None)
                 request.session.pop(SESSION_KEY_PAGE_INFO, None)
             except OSError as e:
                 print(f"Error deleting temporary file {original_temp_path}: {e}")


        # Prepare response
        response = HttpResponse(pdf_data, content_type='application/pdf')
        response['Content-Disposition'] = 'attachment; filename="annotated_document.pdf"'
        return response

    except Exception as e:
        if doc: doc.close()
        print(f"Error processing PDF for download: {e}")
        # Clean up temp file even on error if possible
        original_temp_path = request.session.get(SESSION_KEY_PDF_PATH)
        if original_temp_path and os.path.exists(original_temp_path):
             try:
                 os.unlink(original_temp_path)
             except OSError as e:
                  print(f"Error deleting temporary file {original_temp_path} after download error: {e}")
        # Clear potentially corrupted session data?
        request.session.pop(SESSION_KEY_PDF_PATH, None)
        request.session.pop(SESSION_KEY_ANNOTATIONS, None)
        request.session.pop(SESSION_KEY_PAGE_INFO, None)

        return HttpResponse("Error processing PDF for download.", status=500)