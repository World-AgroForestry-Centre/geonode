# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2012 OpenPlans
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

import os
import sys
import logging
import shutil
import traceback
from guardian.shortcuts import get_perms

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render_to_response
from django.conf import settings
from django.template import RequestContext
from django.utils.translation import ugettext as _
from django.utils import simplejson as json
from django.utils.html import escape
from django.template.defaultfilters import slugify
from django.forms.models import inlineformset_factory
from django.db.models import F
from django.forms.util import ErrorList

from geonode.tasks.deletion import delete_layer
from geonode.services.models import Service
from geonode.layers.forms import LayerForm, LayerUploadForm, NewLayerUploadForm, LayerAttributeForm
from geonode.base.forms import CategoryForm
from geonode.layers.models import Layer, Attribute, UploadSession
from geonode.base.enumerations import CHARSETS
from geonode.base.models import TopicCategory

from geonode.utils import default_map_config
from geonode.utils import GXPLayer
from geonode.utils import GXPMap
from geonode.layers.utils import file_upload, is_raster, is_vector
from geonode.utils import resolve_object, llbbox_to_mercator
from geonode.people.forms import ProfileForm, PocForm
from geonode.security.views import _perms_info_json
from geonode.documents.models import get_related_documents
from geonode.utils import build_social_links
from geonode.geoserver.helpers import cascading_delete, gs_catalog

from icraf_dr.models import Category, Coverage, Source, Year, Main #^^
from dateutil.parser import * #^^
from geonode.layers.models import LayerFile #^^
from django.shortcuts import get_object_or_404 #^^
import csv #^^
from dbfpy import dbf #^^

CONTEXT_LOG_FILE = None

if 'geonode.geoserver' in settings.INSTALLED_APPS:
    from geonode.geoserver.helpers import _render_thumbnail
    from geonode.geoserver.helpers import ogc_server_settings
    CONTEXT_LOG_FILE = ogc_server_settings.LOG_FILE

logger = logging.getLogger("geonode.layers.views")

DEFAULT_SEARCH_BATCH_SIZE = 10
MAX_SEARCH_BATCH_SIZE = 25
GENERIC_UPLOAD_ERROR = _("There was an error while attempting to upload your data. \
Please try again, or contact and administrator if the problem continues.")

_PERMISSION_MSG_DELETE = _("You are not permitted to delete this layer")
_PERMISSION_MSG_GENERIC = _('You do not have permissions for this layer.')
_PERMISSION_MSG_MODIFY = _("You are not permitted to modify this layer")
_PERMISSION_MSG_METADATA = _(
    "You are not permitted to modify this layer's metadata")
_PERMISSION_MSG_VIEW = _("You are not permitted to view this layer")


def log_snippet(log_file):
    if not os.path.isfile(log_file):
        return "No log file at %s" % log_file

    with open(log_file, "r") as f:
        f.seek(0, 2)  # Seek @ EOF
        fsize = f.tell()  # Get Size
        f.seek(max(fsize - 10024, 0), 0)  # Set pos @ last n chars
        return f.read()


def _resolve_layer(request, typename, permission='base.view_resourcebase',
                   msg=_PERMISSION_MSG_GENERIC, **kwargs):
    """
    Resolve the layer by the provided typename (which may include service name) and check the optional permission.
    """
    service_typename = typename.split(":", 1)

    if Service.objects.filter(name=service_typename[0]).exists():
        service = Service.objects.filter(name=service_typename[0])
        return resolve_object(request,
                              Layer,
                              {'service': service[0],
                               'typename': service_typename[1] if service[0].method != "C" else typename},
                              permission=permission,
                              permission_msg=msg,
                              **kwargs)
    else:
        return resolve_object(request,
                              Layer,
                              {'typename': typename,
                               'service': None},
                              permission=permission,
                              permission_msg=msg,
                              **kwargs)


# Basic Layer Views #


@login_required
def layer_upload(request, template='upload/layer_upload.html'):
    if request.method == 'GET':
        
        ##icraf_dr_categories = Category.objects.all() #^^
        icraf_dr_categories = Category.objects.order_by('cat_num') #^^
        ##icraf_dr_coverages = Coverage.objects.all() #^^
        icraf_dr_coverages = Coverage.objects.order_by('cov_num') #^^
        ##icraf_dr_sources = Source.objects.all() #^^
        icraf_dr_sources = Source.objects.order_by('src_num') #^^
        ##icraf_dr_years = Year.objects.all() #^^
        icraf_dr_years = Year.objects.order_by('year_num') #^^
        
        layer_form = LayerForm(prefix="resource") #^^
        category_form = CategoryForm(prefix="category_choice_field") #^^
        
        ctx = {
            'icraf_dr_categories': icraf_dr_categories, #^^
            'icraf_dr_coverages': icraf_dr_coverages, #^^
            'icraf_dr_sources': icraf_dr_sources, #^^
            'icraf_dr_years': icraf_dr_years, #^^
            "layer_form": layer_form, #^^
            "category_form": category_form, #^^
            'charsets': CHARSETS,
            'is_layer': True,
        }
        return render_to_response(template,
                                  RequestContext(request, ctx))
    elif request.method == 'POST':
        form = NewLayerUploadForm(request.POST, request.FILES)
        tempdir = None
        errormsgs = []
        out = {'success': False}

        if form.is_valid():
            title = form.cleaned_data["layer_title"]

            # Replace dots in filename - GeoServer REST API upload bug
            # and avoid any other invalid characters.
            # Use the title if possible, otherwise default to the filename
            if title is not None and len(title) > 0:
                name_base = title
            else:
                name_base, __ = os.path.splitext(
                    form.cleaned_data["base_file"].name)

            name = slugify(name_base.replace(".", "_"))

            try:
                # Moved this inside the try/except block because it can raise
                # exceptions when unicode characters are present.
                # This should be followed up in upstream Django.
                
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
                
                main = Main( #^^
                    category=icraf_dr_category, #^^
                    coverage=icraf_dr_coverage, #^^
                    source=icraf_dr_source, #^^
                    year=icraf_dr_year, #^^
                    basename=name_base, #^^
                    topic_category = TopicCategory(id=request.POST['category_choice_field']), #^^
                    regions = request.POST['regions'], #^^
                    #^^ date_created=icraf_dr_date_created, #^^ 20151019 labels swapped!
                    #^^ date_published=icraf_dr_date_published, #^^ 20151019 labels swapped!
                    date_created=icraf_dr_date_published, #^^
                    date_published=icraf_dr_date_created, #^^
                    date_revised=icraf_dr_date_revised #^^
                ) #^^
                
                #^^ save icraf_dr_main and pass id to file_upload below
                main.save() #^^
                main_id = main.id #^^
                
                #^^ get metadata form values
                form_metadata = json.dumps({ #^^
                    'owner': request.POST['owner'], #^^
                    'title': request.POST['title'], #^^
                    #'date': request.POST['date'], #^^ replaced_by icraf_dr_date_created
                    'date': icraf_dr_date_created, #^^
                    'date_type': request.POST['date_type'], #^^
                    #'edition': request.POST['edition'], #^^ replaced by icraf_dr_year
                    'edition': str(icraf_dr_year.year_num), #^^
                    'abstract': request.POST['abstract'], #^^
                    'purpose': request.POST['purpose'], #^^
                    'maintenance_frequency': request.POST['maintenance_frequency'], #^^
                    'regions': request.POST['regions'], #^^
                    'restriction_code_type': request.POST['restriction_code_type'], #^^
                    'constraints_other': request.POST['constraints_other'], #^^
                    'license': request.POST['license'], #^^
                    'language': request.POST['language'], #^^
                    'spatial_representation_type': request.POST['spatial_representation_type'], #^^
                    'temporal_extent_start': request.POST['temporal_extent_start'], #^^
                    'temporal_extent_end': request.POST['temporal_extent_end'], #^^
                    'supplemental_information': request.POST['supplemental_information'], #^^
                    'distribution_url': request.POST['distribution_url'], #^^
                    'distribution_description': request.POST['distribution_description'], #^^
                    'data_quality_statement': request.POST['data_quality_statement'], #^^
                    'featured': request.POST.get('featured', False), #^^
                    'is_published': request.POST.get('is_published', False), #^^
                    'thumbnail_url': request.POST['thumbnail_url'], #^^
                    'keywords': request.POST['keywords'], #^^
                    'poc': request.POST['poc'], #^^
                    'metadata_author': request.POST['metadata_author'], #^^
                    'category_choice_field': request.POST['category_choice_field'], #^^
		    }) #^^
                
                tempdir, base_file = form.write_files()
                saved_layer = file_upload(
                    base_file,
                    name=name,
                    user=request.user,
                    overwrite=False,
                    charset=form.cleaned_data["charset"],
                    abstract=form.cleaned_data["abstract"],
                    title=form.cleaned_data["layer_title"],
                    main_id=main_id, #^^
                    form_metadata=form_metadata, #^^
                )

            except Exception as e:
                print 'debug layer creation failed, deleting main' #^^
                main.delete() #^^
                
                exception_type, error, tb = sys.exc_info()
                logger.exception(e)
                out['success'] = False
                out['errors'] = str(error)
                # Assign the error message to the latest UploadSession from that user.
                latest_uploads = UploadSession.objects.filter(user=request.user).order_by('-date')
                if latest_uploads.count() > 0:
                    upload_session = latest_uploads[0]
                    upload_session.error = str(error)
                    upload_session.traceback = traceback.format_exc(tb)
                    upload_session.context = log_snippet(CONTEXT_LOG_FILE)
                    upload_session.save()
                    out['traceback'] = upload_session.traceback
                    out['context'] = upload_session.context
                    out['upload_session'] = upload_session.id
            else:
                out['success'] = True
                if hasattr(saved_layer, 'info'):
                    out['info'] = saved_layer.info
                out['url'] = reverse(
                    'layer_detail', args=[
                        saved_layer.service_typename])

                upload_session = saved_layer.upload_session
                upload_session.processed = True
                upload_session.save()
                permissions = form.cleaned_data["permissions"]
                if permissions is not None and len(permissions.keys()) > 0:
                    saved_layer.set_permissions(permissions)

            finally:
                if tempdir is not None:
                    shutil.rmtree(tempdir)
        else:
            for e in form.errors.values():
                errormsgs.extend([escape(v) for v in e])

            out['errors'] = form.errors
            out['errormsgs'] = errormsgs

        if out['success']:
            status_code = 200
        else:
            status_code = 400
        return HttpResponse(
            json.dumps(out),
            mimetype='application/json',
            status=status_code)


def layer_detail(request, layername, template='layers/layer_detail.html'):
    layer = _resolve_layer(
        request,
        layername,
        'base.view_resourcebase',
        _PERMISSION_MSG_VIEW)

    # assert False, str(layer_bbox)
    config = layer.attribute_config()

    # Add required parameters for GXP lazy-loading
    layer_bbox = layer.bbox
    bbox = [float(coord) for coord in list(layer_bbox[0:4])]
    srid = layer.srid

    # Transform WGS84 to Mercator.
    config["srs"] = srid if srid != "EPSG:4326" else "EPSG:900913"
    config["bbox"] = llbbox_to_mercator([float(coord) for coord in bbox])

    config["title"] = layer.title
    config["queryable"] = True

    if layer.storeType == "remoteStore":
        service = layer.service
        source_params = {
            "ptype": service.ptype,
            "remote": True,
            "url": service.base_url,
            "name": service.name}
        maplayer = GXPLayer(
            name=layer.typename,
            ows_url=layer.ows_url,
            layer_params=json.dumps(config),
            source_params=json.dumps(source_params))
    else:
        maplayer = GXPLayer(
            name=layer.typename,
            ows_url=layer.ows_url,
            layer_params=json.dumps(config))

    # Update count for popularity ranking,
    # but do not includes admins or resource owners
    if request.user != layer.owner and not request.user.is_superuser:
        Layer.objects.filter(
            id=layer.id).update(popular_count=F('popular_count') + 1)

    # center/zoom don't matter; the viewer will center on the layer bounds
    map_obj = GXPMap(projection="EPSG:900913")
    NON_WMS_BASE_LAYERS = [
        la for la in default_map_config()[1] if la.ows_url is None]

    metadata = layer.link_set.metadata().filter(
        name__in=settings.DOWNLOAD_FORMATS_METADATA)

    #^^ start check if layer's dbf file is within limits for conversion
    print 'debug'
    MAX_CONVERT_MB = settings.MAX_DOCUMENT_SIZE
    try:
        layer_dbf = LayerFile.objects.get(upload_session=layer.upload_session, name='dbf')
        layer_dbf_path = settings.PROJECT_ROOT + layer_dbf.file.url
        print layer_dbf_path
        if (os.path.getsize(layer_dbf_path) / 1024 / 1024) > MAX_CONVERT_MB:
            layer_dbf = None
    except LayerFile.DoesNotExist:
        layer_dbf = None
    #^^ end
    
    try: #^^
        icraf_dr_main = Main.objects.get(layer=layer) #^^
    except: #^^
        icraf_dr_main = None #^^
    
    context_dict = {
        "resource": layer,
        'perms_list': get_perms(request.user, layer.get_self_resource()),
        "permissions_json": _perms_info_json(layer),
        "documents": get_related_documents(layer),
        "metadata": metadata,
        "is_layer": True,
        "wps_enabled": settings.OGC_SERVER['default']['WPS_ENABLED'],
        'layer_dbf': layer_dbf, #^^
        'icraf_dr_main': icraf_dr_main, #^^
    }

    context_dict["viewer"] = json.dumps(
        map_obj.viewer_json(request.user, * (NON_WMS_BASE_LAYERS + [maplayer])))
    context_dict["preview"] = getattr(
        settings,
        'LAYER_PREVIEW_LIBRARY',
        'leaflet')

    if request.user.has_perm('download_resourcebase', layer.get_self_resource()):
        if layer.storeType == 'dataStore':
            links = layer.link_set.download().filter(
                name__in=settings.DOWNLOAD_FORMATS_VECTOR)
        else:
            links = layer.link_set.download().filter(
                name__in=settings.DOWNLOAD_FORMATS_RASTER)
        context_dict["links"] = links

    if settings.SOCIAL_ORIGINS:
        context_dict["social_links"] = build_social_links(request, layer)

    return render_to_response(template, RequestContext(request, context_dict))

#^^ start
def layer_view(request, layerfile_id):
    layer_dbf = get_object_or_404(LayerFile, pk=layerfile_id)
    
    viewerjs_path = '/static/js/viewerjs/#../../..'
    input_file_path = settings.PROJECT_ROOT + layer_dbf.file.url
    output_dir = 'tmpdoc/'
    output_path = settings.MEDIA_ROOT + '/' + output_dir
    output_format = None
    
    # don't convert if doc file is too big
    MAX_CONVERT_MB = settings.MAX_DOCUMENT_SIZE
    if (os.path.getsize(input_file_path) / 1024 / 1024) > MAX_CONVERT_MB:
        return HttpResponse("Not allowed", status=403)
    
    if input_file_path.lower().endswith('dbf'): # csv format supported by recline.js
        document_title = layer_dbf.name
        document_url = layer_dbf.file.url
        
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
    else:
        return HttpResponse("Not allowed", status=403)
#^^ end
    
@login_required
def layer_metadata(request, layername, template='layers/layer_metadata.html'):
    layer = _resolve_layer(
        request,
        layername,
        'base.change_resourcebase_metadata',
        _PERMISSION_MSG_METADATA)
    layer_attribute_set = inlineformset_factory(
        Layer,
        Attribute,
        extra=0,
        form=LayerAttributeForm,
    )
    topic_category = layer.category

    poc = layer.poc
    metadata_author = layer.metadata_author

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
        
        main, main_created = Main.objects.get_or_create(layer=layer, defaults=main_defaults) #^^
        
        if not main_created: #^^
            main.category = icraf_dr_category #^^
            main.coverage = icraf_dr_coverage #^^
            main.source = icraf_dr_source #^^
            main.year = icraf_dr_year #^^
            main.topic_category = main_topic_category #^^
            main.regions = main_regions #^^
            main.date_created = icraf_dr_date_created #^^ 20151019 label swapped!
            main.date_published = icraf_dr_date_published #^^ 20151019 label swapped!
            main.date_created = icraf_dr_date_published #^^
            main.date_published = icraf_dr_date_created #^^
            main.date_revised = icraf_dr_date_revised #^^
            main.save() #^^
        
        #^^ override resource-date with icraf_dr_date_created
        #^^ override resource-edition with icraf_dr_year
        request_post = request.POST.copy() #^^
        request_post['resource-date'] = icraf_dr_date_created #^^
        request_post['resource-edition'] = icraf_dr_year.year_num #^^
        
        layer_form = LayerForm(request_post, instance=layer, prefix="resource") #^^ replace request.POST
        attribute_form = layer_attribute_set(
            request.POST,
            instance=layer,
            prefix="layer_attribute_set",
            queryset=Attribute.objects.order_by('display_order'))
        category_form = CategoryForm(
            request.POST,
            prefix="category_choice_field",
            initial=int(
                request.POST["category_choice_field"]) if "category_choice_field" in request.POST else None)
    else:
        layer_form = LayerForm(instance=layer, prefix="resource")
        attribute_form = layer_attribute_set(
            instance=layer,
            prefix="layer_attribute_set",
            queryset=Attribute.objects.order_by('display_order'))
        category_form = CategoryForm(
            prefix="category_choice_field",
            initial=topic_category.id if topic_category else None)
        icraf_dr_categories = Category.objects.order_by('cat_num') #^^
        icraf_dr_coverages = Coverage.objects.order_by('cov_num') #^^
        icraf_dr_sources = Source.objects.order_by('src_num') #^^
        icraf_dr_years = Year.objects.order_by('year_num') #^^
        try: #^^
            icraf_dr_main = Main.objects.get(layer=layer) #^^
        except: #^^
            icraf_dr_main = None #^^

    if request.method == "POST" and layer_form.is_valid(
    ) and attribute_form.is_valid() and category_form.is_valid():
        new_poc = layer_form.cleaned_data['poc']
        new_author = layer_form.cleaned_data['metadata_author']
        new_keywords = layer_form.cleaned_data['keywords']

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

        new_category = TopicCategory.objects.get(
            id=category_form.cleaned_data['category_choice_field'])

        for form in attribute_form.cleaned_data:
            la = Attribute.objects.get(id=int(form['id'].id))
            la.description = form["description"]
            la.attribute_label = form["attribute_label"]
            la.visible = form["visible"]
            la.display_order = form["display_order"]
            la.save()

        if new_poc is not None and new_author is not None:
            new_keywords = layer_form.cleaned_data['keywords']
            layer.keywords.clear()
            layer.keywords.add(*new_keywords)
            the_layer = layer_form.save()
            the_layer.poc = new_poc
            the_layer.metadata_author = new_author
            Layer.objects.filter(id=the_layer.id).update(
                category=new_category
                )

            if getattr(settings, 'SLACK_ENABLED', False):
                try:
                    from geonode.contrib.slack.utils import build_slack_message_layer, send_slack_messages
                    send_slack_messages(build_slack_message_layer("layer_edit", the_layer))
                except:
                    print "Could not send slack message."

            return HttpResponseRedirect(
                reverse(
                    'layer_detail',
                    args=(
                        layer.service_typename,
                    )))

    if poc is not None:
        layer_form.fields['poc'].initial = poc.id
        poc_form = ProfileForm(prefix="poc")
        poc_form.hidden = True

    if metadata_author is not None:
        layer_form.fields['metadata_author'].initial = metadata_author.id
        author_form = ProfileForm(prefix="author")
        author_form.hidden = True

    return render_to_response(template, RequestContext(request, {
        "layer": layer,
        "layer_form": layer_form,
        "poc_form": poc_form,
        "author_form": author_form,
        "attribute_form": attribute_form,
        "category_form": category_form,
        'icraf_dr_categories': icraf_dr_categories, #^^
        'icraf_dr_coverages': icraf_dr_coverages, #^^
        'icraf_dr_sources': icraf_dr_sources, #^^
        'icraf_dr_years': icraf_dr_years, #^^
        'icraf_dr_main': icraf_dr_main, #^^
    }))


@login_required
def layer_change_poc(request, ids, template='layers/layer_change_poc.html'):
    layers = Layer.objects.filter(id__in=ids.split('_'))
    if request.method == 'POST':
        form = PocForm(request.POST)
        if form.is_valid():
            for layer in layers:
                layer.poc = form.cleaned_data['contact']
                layer.save()
            # Process the data in form.cleaned_data
            # ...
            # Redirect after POST
            return HttpResponseRedirect('/admin/maps/layer')
    else:
        form = PocForm()  # An unbound form
    return render_to_response(
        template, RequestContext(
            request, {
                'layers': layers, 'form': form}))


@login_required
def layer_replace(request, layername, template='layers/layer_replace.html'):
    layer = _resolve_layer(
        request,
        layername,
        'base.change_resourcebase',
        _PERMISSION_MSG_MODIFY)

    if request.method == 'GET':
        ctx = {
            'charsets': CHARSETS,
            'layer': layer,
            'is_featuretype': layer.is_vector(),
            'is_layer': True,
        }
        return render_to_response(template,
                                  RequestContext(request, ctx))
    elif request.method == 'POST':

        form = LayerUploadForm(request.POST, request.FILES)
        tempdir = None
        out = {}

        if form.is_valid():
            try:
                tempdir, base_file = form.write_files()
                if layer.is_vector() and is_raster(base_file):
                    out['success'] = False
                    out['errors'] = _("You are attempting to replace a vector layer with a raster.")
                elif (not layer.is_vector()) and is_vector(base_file):
                    out['success'] = False
                    out['errors'] = _("You are attempting to replace a raster layer with a vector.")
                else:
                    try: #^^
                        main = Main.objects.get(layer=layer) #^^
                        main_id = main.id #^^
                    except: #^^
                        main_id = None #^^
                    
                    # delete geoserver's store before upload
                    cat = gs_catalog
                    cascading_delete(cat, layer.typename)
                    saved_layer = file_upload(
                        base_file,
                        name=layer.name,
                        user=request.user,
                        overwrite=True,
                        charset=form.cleaned_data["charset"],
                        main_id=main_id, #^^
                    )
                    print 'debug file_upload success'
                    out['success'] = True
                    out['url'] = reverse(
                        'layer_detail', args=[
                            saved_layer.service_typename])
            except Exception as e:
                out['success'] = False
                out['errors'] = str(e)
            finally:
                if tempdir is not None:
                    shutil.rmtree(tempdir)
        else:
            errormsgs = []
            for e in form.errors.values():
                errormsgs.append([escape(v) for v in e])

            out['errors'] = form.errors
            out['errormsgs'] = errormsgs

        if out['success']:
            status_code = 200
        else:
            status_code = 400
        return HttpResponse(
            json.dumps(out),
            mimetype='application/json',
            status=status_code)


@login_required
def layer_remove(request, layername, template='layers/layer_remove.html'):
    layer = _resolve_layer(
        request,
        layername,
        'base.delete_resourcebase',
        _PERMISSION_MSG_DELETE)

    if (request.method == 'GET'):
        return render_to_response(template, RequestContext(request, {
            "layer": layer
        }))
    if (request.method == 'POST'):
        try:
            main = Main.objects.get(layer=layer) #^^
            main.delete() #^^
            delete_layer.delay(object_id=layer.id)
        except Exception as e:
            message = '{0}: {1}.'.format(_('Unable to delete layer'), layer.typename)

            if 'referenced by layer group' in getattr(e, 'message', ''):
                message = _('This layer is a member of a layer group, you must remove the layer from the group '
                            'before deleting.')

            messages.error(request, message)
            return render_to_response(template, RequestContext(request, {"layer": layer}))
        return HttpResponseRedirect(reverse("layer_browse"))
    else:
        return HttpResponse("Not allowed", status=403)


def layer_thumbnail(request, layername):
    if request.method == 'POST':
        layer_obj = _resolve_layer(request, layername)
        try:
            image = _render_thumbnail(request.body)

            if not image:
                return
            filename = "layer-%s-thumb.png" % layer_obj.uuid
            layer_obj.save_thumbnail(filename, image)

            return HttpResponse('Thumbnail saved')
        except:
            return HttpResponse(
                content='error saving thumbnail',
                status=500,
                mimetype='text/plain'
            )
