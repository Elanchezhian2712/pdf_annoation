# annotator/views.py
import fitz  # PyMuPDF
import tempfile
import os
import json
import uuid
import math
from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse, Http404
from django.views.decorators.http import require_POST, require_GET
from django.urls import reverse
from .forms import PDFUploadForm
import traceback

# --- Constants ---
ANNOTATION_TYPES = ['tick', 'cross', 'blue_mark']
SESSION_KEY_PDF_PATH = 'pdf_path'
SESSION_KEY_ANNOTATIONS = 'annotations'
SESSION_KEY_PAGE_INFO = 'page_info'
RENDER_SCALE_FACTOR = 2.0
CLICK_TOLERANCE = 10.0 # PDF points - adjust as needed

# --- Helper Functions --- (No changes needed here)
def get_session_data(request):
    annotations = request.session.get(SESSION_KEY_ANNOTATIONS, [])
    updated = False
    for ann in annotations:
        if 'id' not in ann or not ann['id']:
            ann['id'] = str(uuid.uuid4())
            updated = True
    if updated:
        request.session[SESSION_KEY_ANNOTATIONS] = annotations
    counts = {atype: sum(1 for ann in annotations if ann['type'] == atype) for atype in ANNOTATION_TYPES}
    return annotations, counts

def get_pdf_document(request):
    pdf_path = request.session.get(SESSION_KEY_PDF_PATH)
    if not pdf_path or not os.path.exists(pdf_path):
        return None
    try:
        doc = fitz.open(pdf_path)
        return doc
    except Exception as e:
        print(f"Error opening PDF: {e}")
        return None

# --- Views ---
def upload_pdf(request):
    # ... (no changes needed) ...
    if request.method == 'POST':
        form = PDFUploadForm(request.POST, request.FILES)
        if form.is_valid():
            pdf_file = request.FILES['pdf_file']
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                    for chunk in pdf_file.chunks(): temp_pdf.write(chunk)
                    temp_pdf_path = temp_pdf.name
                request.session[SESSION_KEY_PDF_PATH] = temp_pdf_path
                request.session[SESSION_KEY_ANNOTATIONS] = []
                request.session[SESSION_KEY_PAGE_INFO] = []
                doc = fitz.open(temp_pdf_path)
                page_infos = [{'page_num': i, 'orig_width': page.rect.width, 'orig_height': page.rect.height, 'render_scale': RENDER_SCALE_FACTOR} for i, page in enumerate(doc)]
                request.session[SESSION_KEY_PAGE_INFO] = page_infos
                doc.close()
                return redirect('annotate_pdf')
            except Exception as e:
                 print(f"Error processing uploaded PDF: {e}")
                 traceback.print_exc()
                 if 'temp_pdf_path' in locals() and os.path.exists(temp_pdf_path):
                     try: os.unlink(temp_pdf_path)
                     except OSError as unlink_err: print(f"Error cleaning temp file: {unlink_err}")
                 form.add_error('pdf_file', f'Error processing PDF: {e}')
                 request.session.pop(SESSION_KEY_PDF_PATH, None)
                 request.session.pop(SESSION_KEY_ANNOTATIONS, None)
                 request.session.pop(SESSION_KEY_PAGE_INFO, None)
                 return render(request, 'annotator/upload.html', {'form': form})
    else:
        form = PDFUploadForm()
    return render(request, 'annotator/upload.html', {'form': form})


@require_GET
def annotate_pdf(request):
    # ... (no changes needed) ...
    pdf_path = request.session.get(SESSION_KEY_PDF_PATH)
    page_info = request.session.get(SESSION_KEY_PAGE_INFO)
    if not pdf_path or not page_info: return redirect('upload_pdf')
    annotations, counts = get_session_data(request)
    page_image_urls = [reverse('get_page_image', args=[info['page_num']]) for info in page_info]
    context = { 'page_count': len(page_info), 'page_image_urls': page_image_urls, 'page_info': page_info, 'annotations_json': json.dumps(annotations), 'counts': counts }
    return render(request, 'annotator/annotate.html', context)

@require_GET
def get_page_image(request, page_num):
    # ... (no changes needed) ...
    doc = get_pdf_document(request)
    page_info_list = request.session.get(SESSION_KEY_PAGE_INFO, [])
    if not doc or not isinstance(page_num, int) or page_num < 0 or page_num >= len(doc) or page_num >= len(page_info_list):
        raise Http404("PDF page not found or PDF not loaded.")
    page_info = page_info_list[page_num]
    scale = page_info.get('render_scale', RENDER_SCALE_FACTOR)
    mat = fitz.Matrix(scale, scale)
    try:
        page = doc[page_num]
        pix = page.get_pixmap(matrix=mat, alpha=False)
        doc.close()
        img_data = pix.tobytes("png")
        return HttpResponse(img_data, content_type="image/png")
    except Exception as e:
        if doc: 
            try: doc.close() 
            except: pass
        print(f"Error generating page image {page_num}: {e}")
        raise Http404("Error generating page image.")


# --- MODIFIED add_annotation VIEW ---
@require_POST
def add_annotation(request):
    pdf_path = request.session.get(SESSION_KEY_PDF_PATH)
    page_info_list = request.session.get(SESSION_KEY_PAGE_INFO)
    if not pdf_path or not page_info_list:
        return JsonResponse({'status': 'error', 'message': 'No PDF loaded in session'}, status=400)

    try:
        data = json.loads(request.body)
        page_num = int(data['page_num'])
        natural_x = float(data['x']) # Natural image coords from frontend
        natural_y = float(data['y'])
        annotation_type = data['type'] # The type of the tool clicked
        print(f"Received request: Page={page_num}, Type={annotation_type}, Coords=({natural_x:.1f},{natural_y:.1f})")

        if annotation_type not in ANNOTATION_TYPES:
            return JsonResponse({'status': 'error', 'message': f'Invalid annotation type: {annotation_type}'}, status=400)
        if page_num < 0 or page_num >= len(page_info_list):
             return JsonResponse({'status': 'error', 'message': f'Invalid page number: {page_num}'}, status=400)

        # --- Coordinate Mapping (Natural -> PDF) ---
        pdf_x = natural_x
        pdf_y = natural_y

        # --- Get current annotations (ensuring IDs) ---
        current_annotations, _ = get_session_data(request)
        annotations_updated = False
        action = 'added' # Assume adding first
        removed_id = None
        added_annotation_data = None

        # --- *** GENERALIZED REMOVAL CHECK *** ---
        annotation_to_remove_idx = -1
        min_dist_sq = CLICK_TOLERANCE**2

        print(f"Checking for existing '{annotation_type}' annotations near ({pdf_x:.1f}, {pdf_y:.1f})")
        for i, ann in enumerate(current_annotations):
            # Check if page matches AND type matches the *clicked tool type*
            if ann['page_num'] == page_num and ann['type'] == annotation_type:
                # Calculate squared distance
                dist_sq = (ann['pdf_x'] - pdf_x)**2 + (ann['pdf_y'] - pdf_y)**2
                print(f"  Comparing with Ann ID {ann.get('id', 'N/A')} Type '{ann['type']}' at ({ann['pdf_x']:.1f},{ann['pdf_y']:.1f}). DistSq: {dist_sq:.2f}")
                if dist_sq <= min_dist_sq:
                    # Found an annotation of the same type close enough
                    annotation_to_remove_idx = i
                    removed_id = ann.get('id')
                    print(f"  Found close annotation of type '{annotation_type}' to remove (Index: {i}, ID: {removed_id}).")
                    break # Remove the first one found

        if annotation_to_remove_idx != -1:
            # Remove the item at the found index
            removed_annotation = current_annotations.pop(annotation_to_remove_idx)
            print(f"Removed annotation: {removed_annotation}")
            annotations_updated = True
            action = 'removed'
        # --- *** END GENERALIZED REMOVAL CHECK *** ---


        # --- Add Annotation if no removal occurred ---
        if action == 'added':
            new_annotation_id = str(uuid.uuid4())
            added_annotation_data = {
                'id': new_annotation_id,
                'page_num': page_num,
                'pdf_x': pdf_x,
                'pdf_y': pdf_y,
                'type': annotation_type,
            }
            current_annotations.append(added_annotation_data)
            annotations_updated = True
            print(f"Adding annotation: {added_annotation_data}")


        # --- Update Session and Recalculate Counts ---
        if annotations_updated:
            request.session[SESSION_KEY_ANNOTATIONS] = current_annotations
            _, final_counts = get_session_data(request)
            print(f"Session updated. New counts: {final_counts}")
        else:
            _, final_counts = get_session_data(request)


        # --- Prepare Response ---
        response_data = {
            'status': 'ok',
            'action': action,
            'counts': final_counts
        }
        if action == 'removed':
            response_data['removed_id'] = removed_id
        elif action == 'added':
            response_data['added_annotation'] = added_annotation_data

        print(f"Sending response: {response_data}")
        return JsonResponse(response_data)

    # Error Handling remains the same
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON data received.'}, status=400)
    except (KeyError, ValueError, TypeError) as e:
         print(f"Data error in add_annotation: {e}")
         traceback.print_exc()
         return JsonResponse({'status': 'error', 'message': f'Missing or invalid data field: {e}'}, status=400)
    except Exception as e:
         print(f"Unexpected server error in add_annotation: {e}")
         traceback.print_exc()
         return JsonResponse({'status': 'error', 'message': 'An internal server error occurred.'}, status=500)


@require_GET
def download_pdf(request):
    # ... (no changes needed in download_pdf) ...
    doc = get_pdf_document(request)
    annotations, counts = get_session_data(request)
    if not doc: return HttpResponse("Error: PDF not found or could not be opened.", status=404)
    print(f"Generating download for PDF with {len(annotations)} annotations.")
    static_dir = os.path.join(os.path.dirname(__file__), 'static', 'annotator', 'img')
    icon_paths = { 'tick': os.path.join(static_dir, 'tick.png'), 'cross': os.path.join(static_dir, 'cross.png'), 'blue_mark': os.path.join(static_dir, 'blue_mark.png') }
    icon_size = 15
    try:
        for ann in annotations:
            page_num = ann.get('page_num', -1)
            if 0 <= page_num < len(doc):
                page = doc[page_num]
                pdf_x = ann.get('pdf_x'); pdf_y = ann.get('pdf_y'); ann_type = ann.get('type')
                if pdf_x is None or pdf_y is None or ann_type is None: continue
                icon_path = icon_paths.get(ann_type)
                if icon_path and os.path.exists(icon_path):
                    rect = fitz.Rect(pdf_x - icon_size / 2, pdf_y - icon_size / 2, pdf_x + icon_size / 2, pdf_y + icon_size / 2)
                    try: page.insert_image(rect, filename=icon_path, keep_proportion=True, overlay=True)
                    except Exception as insert_err: print(f"Error inserting image {icon_path} on page {page_num}: {insert_err}")
                else: print(f"Warning: Icon image not found for type '{ann_type}'. Skipping.")
        if len(doc) > 0: # Add counts text
            first_page = doc[0]; text_y = 20
            text = f"Annotation Counts: Ticks={counts.get('tick', 0)}, Crosses={counts.get('cross', 0)}, Blue Marks={counts.get('blue_mark', 0)}"
            text_rect = fitz.Rect(50, text_y, first_page.rect.width - 50, text_y + 20)
            rc = first_page.insert_textbox(text_rect, text, fontsize=10, fontname="helv", align=fitz.TEXT_ALIGN_LEFT)
            if rc < 0: print(f"Warning: Counts text might not have fit.")
        pdf_data = doc.tobytes(garbage=4, deflate=True, clean=True); doc.close()
        print("PDF annotation applied and saved to bytes.")
        original_temp_path = request.session.get(SESSION_KEY_PDF_PATH)
        if original_temp_path and os.path.exists(original_temp_path):
             try:
                 os.unlink(original_temp_path)
                 request.session.pop(SESSION_KEY_PDF_PATH, None); request.session.pop(SESSION_KEY_ANNOTATIONS, None); request.session.pop(SESSION_KEY_PAGE_INFO, None)
                 print(f"Cleaned up temp file and session data: {original_temp_path}")
             except OSError as e: print(f"Error deleting temp file {original_temp_path}: {e}")
        response = HttpResponse(pdf_data, content_type='application/pdf'); response['Content-Disposition'] = 'attachment; filename="annotated_document.pdf"'; return response
    except Exception as e:
        if doc: 
            try: doc.close() 
            except: pass
        print(f"Error processing PDF for download: {e}"); traceback.print_exc()
        original_temp_path = request.session.get(SESSION_KEY_PDF_PATH)
        if original_temp_path and os.path.exists(original_temp_path): 
            try: os.unlink(original_temp_path)
            except OSError: pass
        request.session.pop(SESSION_KEY_PDF_PATH, None); request.session.pop(SESSION_KEY_ANNOTATIONS, None); request.session.pop(SESSION_KEY_PAGE_INFO, None)
        return HttpResponse("Error processing PDF for download.", status=500)