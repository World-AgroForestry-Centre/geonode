import os #^^
import subprocess #^^
import csv #^^
from dbfpy import dbf #^^

import json
from guardian.shortcuts import get_perms

from django.shortcuts import render_to_response, get_object_or_404
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.template import RequestContext, loader
from django.utils.translation import ugettext as _
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.urlresolvers import reverse
from django.core.exceptions import PermissionDenied
from django_downloadview.response import DownloadResponse
from django.views.generic.edit import UpdateView, CreateView
from django.db.models import F
from django.forms.util import ErrorList

from geonode.utils import resolve_object
from geonode.security.views import _perms_info_json
from geonode.people.forms import ProfileForm
from geonode.base.forms import CategoryForm
from geonode.base.models import TopicCategory, ResourceBase
from geonode.documents.models import Document
from geonode.documents.forms import DocumentForm, DocumentCreateForm, DocumentReplaceForm
from geonode.documents.models import IMGTYPES
from geonode.utils import build_social_links

from icraf_dr.models import Category, Coverage, Source, Year, Main #^^
from dateutil.parser import * #^^
from django.core.validators import URLValidator #^^
from django.contrib.auth import get_user_model #^^
from geonode.base.models import SpatialRepresentationType, RestrictionCodeType, License #^^
from django.core.exceptions import ValidationError #^^
from geonode.groups.models import GroupProfile #^^
if 'notification' in settings.INSTALLED_APPS: #^^
    from notification import models as notification #^^

ALLOWED_DOC_TYPES = settings.ALLOWED_DOCUMENT_TYPES

_PERMISSION_MSG_DELETE = _("You are not permitted to delete this document")
_PERMISSION_MSG_GENERIC = _("You do not have permissions for this document.")
_PERMISSION_MSG_MODIFY = _("You are not permitted to modify this document")
_PERMISSION_MSG_METADATA = _(
    "You are not permitted to modify this document's metadata")
_PERMISSION_MSG_VIEW = _("You are not permitted to view this document")


def _resolve_document(request, docid, permission='base.change_resourcebase',
                      msg=_PERMISSION_MSG_GENERIC, **kwargs):
    '''
    Resolve the document by the provided primary key and check the optional permission.
    '''
    return resolve_object(request, Document, {'pk': docid},
                          permission=permission, permission_msg=msg, **kwargs)


def document_detail(request, docid):
    """
    The view that show details of each document
    """
    document = None
    try:
        document = _resolve_document(
            request,
            docid,
            'base.view_resourcebase',
            _PERMISSION_MSG_VIEW)

    except Http404:
        return HttpResponse(
            loader.render_to_string(
                '404.html', RequestContext(
                    request, {
                        })), status=404)

    except PermissionDenied:
        return HttpResponse(
            loader.render_to_string(
                '401.html', RequestContext(
                    request, {
                        'error_message': _("You are not allowed to view this document.")})), status=403)

    if document is None:
        return HttpResponse(
            'An unknown error has occured.',
            mimetype="text/plain",
            status=401
        )

    else:
        try:
            related = document.content_type.get_object_for_this_type(
                id=document.object_id)
        except:
            related = ''

        # Update count for popularity ranking,
        # but do not includes admins or resource owners
        if request.user != document.owner and not request.user.is_superuser:
            Document.objects.filter(id=document.id).update(popular_count=F('popular_count') + 1)

        metadata = document.link_set.metadata().filter(
            name__in=settings.DOWNLOAD_FORMATS_METADATA)

        viewdoc = True #^^
        doc_file_path = document.doc_file.path #^^
        MAX_CONVERT_MB = settings.MAX_DOCUMENT_SIZE #^^
        
        if (os.path.getsize(doc_file_path) / 1024 / 1024) > MAX_CONVERT_MB: #^^
            viewdoc = False #^^
        
        try: #^^
            icraf_dr_main = Main.objects.get(document=document) #^^
        except: #^^
            icraf_dr_main = None #^^
        
        context_dict = {
            'perms_list': get_perms(request.user, document.get_self_resource()),
            'permissions_json': _perms_info_json(document),
            'resource': document,
            'metadata': metadata,
            'imgtypes': IMGTYPES,
            'viewdoc': viewdoc, #^^
            'viewtypes': ['csv', 'xls', 'odt', 'odp', 'ods', 'pdf', 'doc', 'docx', 'ppt', 'pptx', 'xlsx', 'dbf'], #^^
            'icraf_dr_main': icraf_dr_main, #^^
            'related': related}

        if settings.SOCIAL_ORIGINS:
            context_dict["social_links"] = build_social_links(request, document)

        if getattr(settings, 'EXIF_ENABLED', False):
            try:
                from geonode.contrib.exif.utils import exif_extract_dict
                exif = exif_extract_dict(document)
                if exif:
                    context_dict['exif_data'] = exif
            except:
                print "Exif extraction failed."

        return render_to_response(
            "documents/document_detail.html",
            RequestContext(request, context_dict))


def document_download(request, docid):
    document = get_object_or_404(Document, pk=docid)
    if not request.user.has_perm(
            'base.download_resourcebase',
            obj=document.get_self_resource()):
        return HttpResponse(
            loader.render_to_string(
                '401.html', RequestContext(
                    request, {
                        'error_message': _("You are not allowed to view this document.")})), status=401)
    return DownloadResponse(document.doc_file)

#^^ start
def document_view(request, docid):
    document = get_object_or_404(Document, pk=docid)
    if not request.user.has_perm('base.download_resourcebase', obj=document.get_self_resource()):
        return HttpResponse(
            loader.render_to_string(
                '401.html', RequestContext(
                    request, {
                        'error_message': _("You are not allowed to view this document.")}
                )
            ), status=401
        )
    
    viewerjs_path = '/static/js/viewerjs/#../../..'
    input_file_path = document.doc_file.path
    output_dir = 'tmpdoc/'
    output_path = settings.MEDIA_ROOT + '/' + output_dir
    output_format = None
    document_title = document.title
    document_url = document.doc_file.url
    document_extension = document.extension.lower()
    
    # don't convert if doc file is too big, send straight to download
    MAX_CONVERT_MB = settings.MAX_DOCUMENT_SIZE
    if (os.path.getsize(input_file_path) / 1024 / 1024) > MAX_CONVERT_MB:
        return HttpResponseRedirect(document.doc_file.url)
    
    if document_extension in ['csv', 'dbf']: # csv format supported by recline.js
        if document_extension == 'dbf': # convert dbf to csv first
            output_format = 'csv'
            output_file = os.path.basename(os.path.splitext(input_file_path)[0]) + '.' + output_format
            output_file_path = output_path + output_file
            
            with open(output_file_path, 'wb') as csv_file:
                in_db = dbf.Dbf(input_file_path)
                out_csv = csv.writer(csv_file)
                column_header = []
                
                for field in in_db.header.fields:
                    column_header.append(field.name)
                
                out_csv.writerow(column_header)
                
                for rec in in_db:
                    out_csv.writerow(rec.fieldData)
                
                in_db.close()
                document_url = settings.MEDIA_URL + output_dir + output_file
        
        return render_to_response(
            "documents/document_view_recline.html",
            RequestContext(
                request,
                {
                    'document_title': document_title,
                    'document_url': document_url,
                }
            )
        )
    elif document_extension in ['doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx']: # files convertable with unoconv
        if document_extension in ['doc', 'docx', 'ppt', 'pptx']: # better view support in pdf format
            output_format = 'pdf'
        else:
            output_format = 'csv' # 20150921 convert to csv first for reclinejs
            #output_format = 'ods' # better view support for spreadsheets in ods format
        
        output_file = os.path.basename(os.path.splitext(input_file_path)[0]) + '.' + output_format
        output_file_path = output_path + output_file
        
        try:
            subprocess.check_call(['unoconv', '--format', output_format, '--output', output_file_path, input_file_path])
            
            if os.path.exists(output_file_path):
                if document_extension in ['xls', 'xlsx']: # use reclinejs to preview converted xls document
                    document_url = settings.MEDIA_URL + output_dir + output_file
                    
                    return render_to_response(
                        "documents/document_view_recline.html",
                        RequestContext(
                            request,
                            {
                                'document_title': document_title,
                                'document_url': document_url,
                            }
                        )
                    )
                
                return HttpResponseRedirect(viewerjs_path + settings.MEDIA_URL + output_dir + output_file)
            else:
                return HttpResponse('File preview error, please try again.', status=500)
        except subprocess.CalledProcessError as e:
            return HttpResponse('File conversion error: %s' % e, status=500)
    elif document.extension in ['odt', 'odp', 'ods', 'pdf']: # for viewer.js supported files
        return HttpResponseRedirect(viewerjs_path + document.doc_file.url)
    else:
        # just load the file
        return HttpResponseRedirect(document.doc_file.url)
#^^ end

class DocumentUploadView(CreateView):
    template_name = 'documents/document_upload.html'
    form_class = DocumentCreateForm

    def get_context_data(self, **kwargs):
        print 'debug document upload' #^^
        context = super(DocumentUploadView, self).get_context_data(**kwargs)
        context['ALLOWED_DOC_TYPES'] = ALLOWED_DOC_TYPES
        
        icraf_dr_categories = Category.objects.order_by('cat_num') #^^
        icraf_dr_coverages = Coverage.objects.order_by('cov_num') #^^
        icraf_dr_sources = Source.objects.order_by('src_num') #^^
        icraf_dr_years = Year.objects.order_by('year_num') #^^
        
        document_form = DocumentForm(prefix="resource") #^^
        category_form = CategoryForm(prefix="category_choice_field") #^^
        
        context['icraf_dr_categories'] = icraf_dr_categories #^^
        context['icraf_dr_coverages'] = icraf_dr_coverages #^^
        context['icraf_dr_sources'] = icraf_dr_sources #^^
        context['icraf_dr_years'] = icraf_dr_years #^^
        context['document_form'] = document_form #^^
        context['category_form'] = category_form #^^
        
        return context

    def form_valid(self, form):
        """
        If the form is valid, save the associated model.
        """
        print 'debug document form valid' #^^
        
        icraf_dr_category =Category.objects.get(pk=self.request.POST['icraf_dr_category']) #^^
        icraf_dr_coverage =Coverage.objects.get(pk=self.request.POST['icraf_dr_coverage']) #^^
        icraf_dr_source =Source.objects.get(pk=self.request.POST['icraf_dr_source']) #^^
        icraf_dr_year =Year.objects.get(pk=self.request.POST['icraf_dr_year']) #^^
        icraf_dr_date_created = self.request.POST.get('icraf_dr_date_created', None) #^^
        icraf_dr_date_published = self.request.POST.get('icraf_dr_date_published', None) #^^
        icraf_dr_date_revised = self.request.POST.get('icraf_dr_date_revised', None) #^^
        
        #^^ validate date format
        if icraf_dr_date_created: #^^
            try: #^^
                parse(icraf_dr_date_created) #^^
            except ValueError: #^^
                icraf_dr_date_created = None #^^
        else: #^^
            icraf_dr_date_created = None #^^
        
        if icraf_dr_date_published: #^^
            try: #^^
                parse(icraf_dr_date_published) #^^
            except ValueError: #^^
                icraf_dr_date_published = None #^^
        else: #^^
            icraf_dr_date_published = None #^^
        
        if icraf_dr_date_revised: #^^
            try: #^^
                parse(icraf_dr_date_revised) #^^
            except ValueError: #^^
                icraf_dr_date_revised = None #^^
        else: #^^
            icraf_dr_date_revised = None #^^
        
        try: #^^
            main_topic_category = TopicCategory(id=self.request.POST['category_choice_field']) #^^
        except: #^^
            main_topic_category = None #^^
        
        main_regions = ','.join(self.request.POST.getlist('resource-regions')) #^^ save as comma separated ids
        
        main = Main( #^^
            category=icraf_dr_category, #^^
            coverage=icraf_dr_coverage, #^^
            source=icraf_dr_source, #^^
            year=icraf_dr_year, #^^
            basename=form.cleaned_data['doc_file'].name, #^^
            topic_category = main_topic_category, #^^
            regions = main_regions, #^^
            #^^ date_created=icraf_dr_date_created, #^^ 20151019 label swapped!
            #^^ date_published=icraf_dr_date_published, #^^ 20151019 label swapped!
            date_created=icraf_dr_date_published, #^^
            date_published=icraf_dr_date_created, #^^
            date_revised=icraf_dr_date_revised #^^
        ) #^^
        
        #^^ save icraf_dr_main and pass id to document model object
        main.save() #^^
        main_id = main.id #^^
        
        self.object = form.save(commit=False)
        self.object.owner = self.request.user
        resource_id = self.request.POST.get('resource', None)
        if resource_id:
            self.object.content_type = ResourceBase.objects.get(id=resource_id).polymorphic_ctype
            self.object.object_id = resource_id
        # by default, if RESOURCE_PUBLISHING=True then document.is_published
        # must be set to False
        is_published = True
        if settings.RESOURCE_PUBLISHING:
            is_published = False
        self.object.is_published = is_published
        
        self.object.main_id = main_id #^^
        
        try: #^^
            self.object.save() #^^
            main.document = self.object #^^
            main.save() #^^
        except: #^^
            main.delete() #^^
        
        #^^ self.object.save()
        #^^self.object.set_permissions(form.cleaned_data['permissions'])
        
        document = None #^^
        try: #^^
            #^^ alternate permission saving method
            print 'debug' #^^
            print self.object.id #^^
            document = Document.objects.get(id=self.object.id) #^^
            document.set_permissions(form.cleaned_data['permissions']) #^^
        except: #^^
            pass #^^

        abstract = None
        date = None
        regions = []
        keywords = []
        bbox = None
        
        #^^ start processing metadata fields
        owner = self.request.POST.get('resource-owner', None) #^^
        #title = self.request.POST['resource-title'] #^^ replaced by title
        #date = self.request.POST.get('resource-date'], None) #^^ replaced by icraf_dr_date_created
        date = icraf_dr_date_created #^^
        date_type = self.request.POST.get('resource-date_type', None) #^^
        #edition = self.request.POST['resource-edition'] #^^ replaced by icraf_dr_year
        edition = str(icraf_dr_year.year_num) #^^
        abstract = self.request.POST.get('resource-abstract', None) #^^
        purpose = self.request.POST.get('resource-purpose', None) #^^
        maintenance_frequency = self.request.POST.get('resource-maintenance_frequency', None) #^^
        regions = self.request.POST.getlist('resource-regions', None) #^^
        restriction_code_type = self.request.POST.get('resource-restriction_code_type', None) #^^
        constraints_other = self.request.POST.get('resource-constraints_other', None) #^^
        license = self.request.POST.get('resource-license', None) #^^
        language = self.request.POST.get('resource-language', None) #^^
        spatial_representation_type = self.request.POST.get('resource-spatial_representation_type', None) #^^
        temporal_extent_start = self.request.POST.get('resource-temporal_extent_start', None) #^^
        temporal_extent_end = self.request.POST.get('resource-temporal_extent_end', None) #^^
        supplemental_information = self.request.POST.get('resource-supplemental_information', None) #^^
        distribution_url = self.request.POST.get('resource-distribution_url', None) #^^
        distribution_description = self.request.POST.get('resource-distribution_description', None) #^^
        data_quality_statement = self.request.POST.get('resource-data_quality_statement', None) #^^
        featured = self.request.POST.get('resource-featured', False) #^^
        is_published = self.request.POST.get('resource-is_published', False) #^^
        thumbnail_url = self.request.POST.get('resource-thumbnail_url', None) #^^
        keywords = self.request.POST.get('resource-keywords', None) #^^
        poc = self.request.POST.get('resource-poc', None) #^^
        metadata_author = self.request.POST.get('resource-metadata_author', None) #^^
        category_choice_field = self.request.POST.get('category_choice_field', None) #^^
        doc_type = self.request.POST.get('doc_type', None) #^^
        
        if owner and owner.isdigit():
            try:
                owner = get_user_model().objects.get(id=owner)
                self.object.owner = owner
            except get_user_model().DoesNotExist:
                pass
        
        if date:
            self.object.date = date
        
        if date_type:
            self.object.date_type = date_type
        
        if edition:
            self.object.edition = edition
        
        if abstract:
            self.object.abstract = abstract
        
        if purpose:
            self.object.purpose = purpose
        
        if maintenance_frequency:
            self.object.maintenance_frequency = maintenance_frequency
        
        if restriction_code_type:
            try:
                self.object.restriction_code_type = RestrictionCodeType(id=restriction_code_type)
            except:
                pass
        
        if constraints_other:
            self.object.constraints_other = constraints_other
        
        if license:
            try:
                self.object.license = License(id=license)
            except:
                pass
        
        if language:
            self.object.language = language
        
        if spatial_representation_type:
            try:
                self.object.spatial_representation_type = SpatialRepresentationType(id=spatial_representation_type)
            except:
                pass
        
        if temporal_extent_start:
            try:
                parse(temporal_extent_start)
                self.object.temporal_extent_start = temporal_extent_start
            except ValueError:
                pass
        
        if temporal_extent_end:
            try:
                parse(temporal_extent_end)
                self.object.temporal_extent_end = temporal_extent_end
            except ValueError:
                pass
        
        if supplemental_information:
            self.object.supplemental_information = supplemental_information
        
        if distribution_url:
            self.object.distribution_url = distribution_url
        
        if distribution_description:
            self.object.distribution_description = distribution_description
        
        if data_quality_statement:
            self.object.data_quality_statement = data_quality_statement
        
        if featured != False:
            self.object.featured = True
        
        if is_published != False:
            self.object.is_published = True
        
        if thumbnail_url:
            val = URLValidator()
            try:
                val(thumbnail_url)
                if (thumbnail_url.lower().startswith(('http://', 'https://')) and thumbnail_url.lower().endswith(('.jpg', '.jpeg', '.png'))):
                    self.object.thumbnail_url = thumbnail_url
            except ValidationError:
                pass
        
        if len(keywords) > 0 and document != None:
            keywords = [keyword.strip() for keyword in keywords.split(',')]
            document.keywords.add(*keywords)
        
        if len(regions) > 0 and document != None:
            document.regions.add(*regions)
        
        if poc and poc.isdigit():
            try:
                contact = get_user_model().objects.get(id=poc)
                self.object.poc = contact
            except get_user_model().DoesNotExist:
                pass
        
        if metadata_author and metadata_author.isdigit():
            try:
                author = get_user_model().objects.get(id=metadata_author)
                self.object.metadata_author = author
            except get_user_model().DoesNotExist:
                pass
        
        if category_choice_field:
            try:
                self.object.category = TopicCategory(id=category_choice_field)
            except:
                pass
        
        self.object.save()
        
        if doc_type:
            try:
                Document.objects.filter(id=self.object.pk).update(doc_type=doc_type)
            except:
                pass
        #^^ end

        if getattr(settings, 'EXIF_ENABLED', False):
            try:
                from geonode.contrib.exif.utils import exif_extract_metadata_doc
                exif_metadata = exif_extract_metadata_doc(self.object)
                if exif_metadata:
                    date = exif_metadata.get('date', None)
                    keywords.extend(exif_metadata.get('keywords', []))
                    bbox = exif_metadata.get('bbox', None)
                    abstract = exif_metadata.get('abstract', None)
            except:
                print "Exif extraction failed."

        if getattr(settings, 'NLP_ENABLED', False):
            try:
                from geonode.contrib.nlp.utils import nlp_extract_metadata_doc
                nlp_metadata = nlp_extract_metadata_doc(self.object)
                if nlp_metadata:
                    regions.extend(nlp_metadata.get('regions', []))
                    keywords.extend(nlp_metadata.get('keywords', []))
            except:
                print "NLP extraction failed."

        """ #^^ overriden above in metadata saving
        if abstract:
            self.object.abstract = abstract
            self.object.save()

        if date:
            self.object.date = date
            self.object.date_type = "Creation"
            self.object.save()

        if len(regions) > 0:
            self.object.regions.add(*regions)

        if len(keywords) > 0:
            self.object.keywords.add(*keywords)
        """

        if bbox:
            bbox_x0, bbox_x1, bbox_y0, bbox_y1 = bbox
            Document.objects.filter(id=self.object.pk).update(
                bbox_x0=bbox_x0,
                bbox_x1=bbox_x1,
                bbox_y0=bbox_y0,
                bbox_y1=bbox_y1)

        if getattr(settings, 'SLACK_ENABLED', False):
            try:
                from geonode.contrib.slack.utils import build_slack_message_document, send_slack_message
                send_slack_message(build_slack_message_document("document_new", self.object))
            except:
                print "Could not send slack message for new document."

        #^^return HttpResponseRedirect(
        #^^    reverse(
        #^^        'document_metadata',
        #^^        args=(
        #^^            self.object.id,
        #^^        )))
        
        #^^ start send email notification to 'Pengelola Basis Data' group managers
        if 'notification' in settings.INSTALLED_APPS and document != None:
            try:
                group_approver = GroupProfile.objects.get(title='Pengelola Basis Data')
                if group_approver:
                    group_managers = group_approver.get_managers()
                    if group_managers:
                        notif_ctx = {
                            'resource': document,
                        }
                        print 'sending notification'
                        notification.send(group_managers, 'document_created', notif_ctx)
            except:
                pass
        #^^ end

        #^^ redirect to document detail instead of document metadata page
        return HttpResponseRedirect( #^^
            reverse( #^^
                'document_detail', #^^
                args=( #^^
                    self.object.id, #^^
                ))) #^^


class DocumentUpdateView(UpdateView):
    template_name = 'documents/document_replace.html'
    pk_url_kwarg = 'docid'
    form_class = DocumentReplaceForm
    queryset = Document.objects.all()
    context_object_name = 'document'

    def get_context_data(self, **kwargs):
        context = super(DocumentUpdateView, self).get_context_data(**kwargs)
        context['ALLOWED_DOC_TYPES'] = ALLOWED_DOC_TYPES
        return context

    def form_valid(self, form):
        """
        If the form is valid, save the associated model.
        """
        self.object = form.save(commit=False) #^^
        
        try: #^^
            main = Main.objects.get(document=self.object) #^^
            main_id = main.id #^^
            self.object.main_id = main_id #^^
            main.basename = form.cleaned_data['doc_file'].name #^^
            main.save() #^^
        except: #^^
            pass #^^
        
        self.object.save() #^^
        
        #^^self.object = form.save()
        #^^return HttpResponseRedirect(
        #^^    reverse(
        #^^        'document_metadata',
        #^^        args=(
        #^^            self.object.id,
        #^^        )))
        
        #^^ redirect to document detail instead of document metadata page
        return HttpResponseRedirect( #^^
            reverse( #^^
                'document_detail', #^^
                args=( #^^
                    self.object.id, #^^
                ))) #^^


@login_required
def document_metadata(
        request,
        docid,
        template='documents/document_metadata.html'):

    document = None
    try:
        document = _resolve_document(
            request,
            docid,
            'base.change_resourcebase_metadata',
            _PERMISSION_MSG_METADATA)

    except Http404:
        return HttpResponse(
            loader.render_to_string(
                '404.html', RequestContext(
                    request, {
                        })), status=404)

    except PermissionDenied:
        return HttpResponse(
            loader.render_to_string(
                '401.html', RequestContext(
                    request, {
                        'error_message': _("You are not allowed to edit this document.")})), status=403)

    if document is None:
        return HttpResponse(
            'An unknown error has occured.',
            mimetype="text/plain",
            status=401
        )

    else:
        poc = document.poc
        metadata_author = document.metadata_author
        topic_category = document.category

        if request.method == "POST":
            icraf_dr_category =Category.objects.get(pk=request.POST['icraf_dr_category']) #^^
            icraf_dr_coverage =Coverage.objects.get(pk=request.POST['icraf_dr_coverage']) #^^
            icraf_dr_source =Source.objects.get(pk=request.POST['icraf_dr_source']) #^^
            icraf_dr_year =Year.objects.get(pk=request.POST['icraf_dr_year']) #^^
            icraf_dr_date_created = request.POST['icraf_dr_date_created'] #^^
            icraf_dr_date_published = request.POST['icraf_dr_date_published'] #^^
            icraf_dr_date_revised = request.POST['icraf_dr_date_revised'] #^^
            
            #^^ validate date format
            if (len(icraf_dr_date_created)): #^^
                try: #^^
                    parse(icraf_dr_date_created) #^^
                except ValueError: #^^
                    icraf_dr_date_created = None #^^
            else: #^^
                icraf_dr_date_created = None #^^
            
            if (len(icraf_dr_date_published)): #^^
                try: #^^
                    parse(icraf_dr_date_published) #^^
                except ValueError: #^^
                    icraf_dr_date_published = None #^^
            else: #^^
                icraf_dr_date_published = None #^^
            
            if (len(icraf_dr_date_revised)): #^^
                try: #^^
                    parse(icraf_dr_date_revised) #^^
                except ValueError: #^^
                    icraf_dr_date_revised = None #^^
            else: #^^
                icraf_dr_date_revised = None #^^
            
            try: #^^
                main_topic_category = TopicCategory(id=request.POST['category_choice_field']) #^^
            except: #^^
                main_topic_category = None #^^
            
            main_regions = ','.join(request.POST.getlist('resource-regions')) #^^ save as comma separated ids
            
            main_defaults = { #^^
                'category': icraf_dr_category, #^^
                'coverage': icraf_dr_coverage, #^^
                'source': icraf_dr_source, #^^
                'year': icraf_dr_year, #^^
                'topic_category': main_topic_category, #^^
                'regions': main_regions, #^^
                #^^ 'date_created': icraf_dr_date_created, #^^ 20151019 label swapped!
                #^^ 'date_published': icraf_dr_date_published, #^^ 20151019 label swapped!
                'date_created': icraf_dr_date_published, #^^
                'date_published': icraf_dr_date_created, #^^
                'date_revised': icraf_dr_date_revised #^^
            } #^^
            
            main, main_created = Main.objects.get_or_create(document=document, defaults=main_defaults) #^^
            
            if not main_created: #^^
                main.category = icraf_dr_category #^^
                main.coverage = icraf_dr_coverage #^^
                main.source = icraf_dr_source #^^
                main.year = icraf_dr_year #^^
                main.topic_category = main_topic_category #^^
                main.regions = main_regions #^^
                #^^ main.date_created = icraf_dr_date_created #^^ 20151019 label swapped!
                #^^ main.date_published = icraf_dr_date_published #^^ 20151019 label swapped!
                main.date_created = icraf_dr_date_published #^^
                main.date_published = icraf_dr_date_created #^^
                main.date_revised = icraf_dr_date_revised #^^
                main.save() #^^
            
            #^^ override resource-date with icraf_dr_date_created
            #^^ override resource-edition with icraf_dr_year
            request_post = request.POST.copy() #^^
            request_post['resource-date'] = icraf_dr_date_created #^^
            request_post['resource-edition'] = icraf_dr_year.year_num #^^
            
            document_form = DocumentForm(
                request_post, #^^ replace request.POST
                instance=document,
                prefix="resource")
            category_form = CategoryForm(
                request.POST,
                prefix="category_choice_field",
                initial=int(
                    request.POST["category_choice_field"]) if "category_choice_field" in request.POST else None)
        else:
            document_form = DocumentForm(instance=document, prefix="resource")
            category_form = CategoryForm(
                prefix="category_choice_field",
                initial=topic_category.id if topic_category else None)
            
            icraf_dr_categories = Category.objects.order_by('cat_num') #^^
            icraf_dr_coverages = Coverage.objects.order_by('cov_num') #^^
            icraf_dr_sources = Source.objects.order_by('src_num') #^^
            icraf_dr_years = Year.objects.order_by('year_num') #^^
            try: #^^
                icraf_dr_main = Main.objects.get(document=document) #^^
            except: #^^
                icraf_dr_main = None #^^

        if request.method == "POST" and document_form.is_valid(
        ) and category_form.is_valid():
            new_poc = document_form.cleaned_data['poc']
            new_author = document_form.cleaned_data['metadata_author']
            new_keywords = document_form.cleaned_data['keywords']
            new_category = TopicCategory.objects.get(
                id=category_form.cleaned_data['category_choice_field'])

            if new_poc is None:
                if poc is None:
                    poc_form = ProfileForm(
                        request.POST,
                        prefix="poc",
                        instance=poc)
                else:
                    poc_form = ProfileForm(request.POST, prefix="poc")
                if poc_form.is_valid():
                    if len(poc_form.cleaned_data['profile']) == 0:
                        # FIXME use form.add_error in django > 1.7
                        errors = poc_form._errors.setdefault('profile', ErrorList())
                        errors.append(_('You must set a point of contact for this resource'))
                        poc = None
                if poc_form.has_changed and poc_form.is_valid():
                    new_poc = poc_form.save()

            if new_author is None:
                if metadata_author is None:
                    author_form = ProfileForm(request.POST, prefix="author",
                                              instance=metadata_author)
                else:
                    author_form = ProfileForm(request.POST, prefix="author")
                if author_form.is_valid():
                    if len(author_form.cleaned_data['profile']) == 0:
                        # FIXME use form.add_error in django > 1.7
                        errors = author_form._errors.setdefault('profile', ErrorList())
                        errors.append(_('You must set an author for this resource'))
                        metadata_author = None
                if author_form.has_changed and author_form.is_valid():
                    new_author = author_form.save()

            if new_poc is not None and new_author is not None:
                the_document = document_form.save()
                the_document.poc = new_poc
                the_document.metadata_author = new_author
                the_document.keywords.add(*new_keywords)
                Document.objects.filter(id=the_document.id).update(category=new_category)
                
                #^^ start update doc_type
                doc_type = request.POST.get('doc_type', None) #^^
                
                if doc_type:
                    try:
                        Document.objects.filter(id=the_document.id).update(doc_type=doc_type)
                    except:
                        pass
                #^^ end

                if getattr(settings, 'SLACK_ENABLED', False):
                    try:
                        from geonode.contrib.slack.utils import build_slack_message_document, send_slack_messages
                        send_slack_messages(build_slack_message_document("document_edit", the_document))
                    except:
                        print "Could not send slack message for modified document."

                return HttpResponseRedirect(
                    reverse(
                        'document_detail',
                        args=(
                            document.id,
                        )))

        if poc is not None:
            document_form.fields['poc'].initial = poc.id
            poc_form = ProfileForm(prefix="poc")
            poc_form.hidden = True

        if metadata_author is not None:
            document_form.fields['metadata_author'].initial = metadata_author.id
            author_form = ProfileForm(prefix="author")
            author_form.hidden = True

        return render_to_response(template, RequestContext(request, {
            "document": document,
            "document_form": document_form,
            "poc_form": poc_form,
            "author_form": author_form,
            "category_form": category_form,
            'icraf_dr_categories': icraf_dr_categories, #^^
            'icraf_dr_coverages': icraf_dr_coverages, #^^
            'icraf_dr_sources': icraf_dr_sources, #^^
            'icraf_dr_years': icraf_dr_years, #^^
            'icraf_dr_main': icraf_dr_main, #^^
        }))


def document_search_page(request):
    # for non-ajax requests, render a generic search page

    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    return render_to_response(
        'documents/document_search.html',
        RequestContext(
            request,
            {
                'init_search': json.dumps(
                    params or {}),
                "site": settings.SITEURL}))


@login_required
def document_remove(request, docid, template='documents/document_remove.html'):
    try:
        document = _resolve_document(
            request,
            docid,
            'base.delete_resourcebase',
            _PERMISSION_MSG_DELETE)

        if request.method == 'GET':
            return render_to_response(template, RequestContext(request, {
                "document": document
            }))

        if request.method == 'POST':

            try: #^^
                main = Main.objects.get(document=document) #^^
                main.delete() #^^
            except: #^^
                pass #^^
            
            if getattr(settings, 'SLACK_ENABLED', False):
                slack_message = None
                try:
                    from geonode.contrib.slack.utils import build_slack_message_document
                    slack_message = build_slack_message_document("document_delete", document)
                except:
                    print "Could not build slack message for delete document."

                document.delete()

                try:
                    from geonode.contrib.slack.utils import send_slack_messages
                    send_slack_messages(slack_message)
                except:
                    print "Could not send slack message for delete document."
            else:
                document.delete()

            return HttpResponseRedirect(reverse("document_browse"))
        else:
            return HttpResponse("Not allowed", status=403)

    except PermissionDenied:
        return HttpResponse(
            'You are not allowed to delete this document',
            mimetype="text/plain",
            status=401
        )
