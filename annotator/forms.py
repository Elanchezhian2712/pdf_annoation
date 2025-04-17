# annotator/forms.py
from django import forms

class PDFUploadForm(forms.Form):
    pdf_file = forms.FileField(label='Select a PDF file', required=True,
                               widget=forms.ClearableFileInput(attrs={'accept': '.pdf'}))