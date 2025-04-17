# annotator/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('', views.upload_pdf, name='upload_pdf'),
    path('annotate/', views.annotate_pdf, name='annotate_pdf'),
    path('download/', views.download_pdf, name='download_pdf'),
    path('add_annotation/', views.add_annotation, name='add_annotation'), # AJAX endpoint
    path('page_image/<int:page_num>/', views.get_page_image, name='get_page_image'),
]