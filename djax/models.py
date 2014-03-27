"""
Models for Djax.
"""
from django.db import models, IntegrityError
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import User
import logging
from datetime import datetime
from djax.gateway import content_client, library_client, library_project, trigger_client
from djax.registry import content_registry, build_registry
import re
import threading
import uuid

log = logging.getLogger('djax')

class DefaultFieldConverter(object):
    def __init__(self,local_field):
        self.field = local_field
    
    def to_local_model(self,ace_content,ace_field_value):
        """
        Associated content link, if found, otherwise just assigns value.
        """
        if re.match(r'^[\w\s]+:[A-Fa-f0-9]+$',ace_field_value):
            ctype, ckey = ace_field_value.split(':')
            record = ContentRecord.objects.get(axilent_content_type=ctype,axilent_content_key=ckey)
            log.debug('Converted content link %s to local model.' % ace_field_value)
            return record.get_local_model()
        else:
            return ace_field_value
    
    def to_ace(self,local_model):
        """
        Returns the content link string from the model.
        """
        value = getattr(local_model,self.field)
        if isinstance(value,models.Model):
            try:
                record = ContentRecord.objects.get_record(value)
                log.debug('Converted local model to content link %s:%s.' % (record.axilent_content_type,record.axilent_content_key))
                return '%s:%s' % (record.axilent_content_type,record.axilent_content_key)
            except AxilentContentRecord.DoesNotExist:
                return ''
        else:
            return value

class AxilentContentRecordManager(models.Manager):
    """
    Manager class for AxilentContentRecord.
    """
    def __init__(self,*args,**kwargs):
        super(AxilentContentRecordManager,self).__init__(*args,**kwargs)
        self.lock = threading.RLock()
    
    def get_record(self,model):
        """
        Gets the record for the specified model.
        """
        content_type = ContentType.objects.get_for_model(model)
        return self.get(local_content_type=content_type,local_id=model.pk)
    
    def field_map(self,model,axilent_content={}):
        """
        Gets the field map for the model.
        """
        field_map = {}
        excludes = []
        
        # collect mappings
        if hasattr(model,'Axilent'):
            if hasattr(model.Axilent,'field_map'):
                field_map = model.Axilent.field_map
            
            if hasattr(model.Axilent,'exclude'):
                excludes = model.Axilent.exclude
        
        # Default the field map to the axilent fields
        if not field_map:
            for key in axilent_content.data.keys():
                if not key in excludes:
                    field_map[key] = key
        
        return field_map
    
    def create_model(self,axilent_content_type,axilent_content_key):
        """
        Creates a new model and accompaning content record for the axilent content.
        """
        content_data = content_client.get_content(axilent_content_type,axilent_content_key)
        print 'creating model with data',content_data.data
        local_model, record = None, None
        try:
            model_class = content_registry[axilent_content_type]
        
            field_map = {}
            try:
                field_map = model_class.Axilent.field_map
            except AttributeError:
                for key in content_data.data.keys():
                    field_map[key] = key
            
            # Iterate through the field map and set the local model values from the incoming Axilent content
            fields = {}
            deferred_field_converters = []
            for axilent_field, model_field in field_map.items():
                print 'settings ace field',axilent_field,'with value',getattr(content_data,axilent_field)
                try:            
                    if hasattr(model_field,'field'):
                        # this is a field converter
                        # sanity check
                        if not hasattr(model_field,'to_ace') or not hasattr(model_field,'to_local_model'):
                            raise ValueError('You must define the methods to_ace and to_local_model for field converter for ace field %s.' % axilent_field)
                
                        if hasattr(model_field,'deferred') and model_field.deferred:
                            print 'adding ace field',axilent_field,'and model field',model_field,'to deferred list'
                            deferred_field_converters.append((axilent_field,model_field))
                        else:
                            value = model_field.to_local_model(content_data,getattr(content_data,axilent_field))
                            print 'assigning value',value,'from ace field',axilent_field,'to local field',model_field
                            fields[model_field.field] = value
                    else:
                        # not a field converter, just a string.  Use DefaultFieldConverter
                        print 'assigning ace field',axilent_field,'to local field',model_field,'with default field converter'
                        default_field_converter = DefaultFieldConverter(model_field)
                        fields[model_field] = default_field_converter.to_local_model(content_data,getattr(content_data,axilent_field))
                except AttributeError:
                    log.exception('Local model has no field %s (matched to Axilent field %s).' % (model_field,axilent_field))
            
            local_model = model_class.objects.create(**fields) # create the local model with the content data
            
            if deferred_field_converters:
                print 'running deferred field converters...'
                for deferred_axilent_field, deferred_model_field in deferred_field_converters:
                    print 'assigning ace field',deferred_axilent_field,'to local model field',deferred_model_field,'deferred'
                    try:
                        deferred_model_field.to_local_model(content_data,getattr(content_data,axilent_field),local_model)
                    except AttributeError:
                        log.exception('Local model has no field %s (matched to Axilent field %s).' % (deferred_model_field.field,deferred_axilent_field))
            
                local_model.save()
            else:
                log.info('No deferred field converters for %s.' % unicode(local_model))

            local_content_type = ContentType.objects.get_for_model(local_model)
            record = self.create(local_content_type=local_content_type,
                                 local_id=local_model.pk,
                                 axilent_content_type=axilent_content_type,
                                 axilent_content_key=axilent_content_key,
                                 updated=datetime.now())
        except KeyError:
            raise ValueError('ACE content type %s cannot be found in the local registry.' % axilent_content_type)
        
        return (local_model,record)
    
    def model_to_content_link(self,value):
        """
        Converts a local model (held as a foreign key reference) to an Axilent
        content link in <content-type>:<content-key> format.
        """
        if isinstance(value,models.Model):
            try:
                record = self.get_record(value)
                return '%s:%s' % (record.axilent_content_type,record.axilent_content_key)
            except AxilentContentRecord.DoesNotExist:
                return ''
        else:
            return value
    
    def content_link_to_model(self,value):
        """
        Converts a content link format string '<content-type>:<content-key>' to a local
        model to be used as a foreign key.
        """
        if re.match(r'^[\w\s]+:[A-Fa-f0-9]+$',value):
            ctype, ckey = value.split(':')
            record = self.get(axilent_content_type=ctype,axilent_content_key=ckey)
            return record.get_local_model()
        else:
            return value
    
    def data_for_library(self,model):
        """
        Gets a data dictionary prepared for the library.
        """
        lib_data = {}
        field_map = self.field_map(model)
        for axilent_field, model_field in field_map.items():
            try:
                lib_data[axilent_field] = self.model_to_content_link(getattr(model,model_field))
            except AttributeError:
                log.exception('Local model has no field %s (matched to Axilent field %s).' % (model_field,axilent_field))
        
        return lib_data
    
    def push_to_library(self,model):
        """
        Pushes the model to the Axilent library (assuming the library integration is active).
        
        Returns a 2-tuple of booleans indicating 1.  If the library was updated and 2. If the
        content item was created on Axilent for the first time.
        
        """
        if library_client:
            with self.lock:
                lib_data = self.data_for_library(model)
                try:
                    record = self.get_record(model)
                    # this content item already exists on Axilent - update
                    response = library_client.update_content(record.axilent_content_type,
                                                             library_project,
                                                             record.axilent_content_key,
                                                             **lib_data)
                    return (True,False)
                except AxilentContentRecord.DoesNotExist:
                    # this is new
                    local_content_type = ContentType.objects.get_for_model(model)
                    axilent_content_type = model.Axilent.content_type
                    response = library_client.create_content(axilent_content_type,
                                                             library_project,
                                                             **lib_data)
                    returned_content_type, returned_key = response.split(':')
                
                    # create new record
                    self.create(local_content_type=local_content_type,
                                local_id=model.pk,
                                axilent_content_type=axilent_content_type,
                                axilent_content_key=returned_key)
            
                    return (True,True)

        else:
            return (False,False)
    
    def push_to_graphstack(self,model):
        """
        Pushes the model to the graphstack associated with the content client.  Like push_to_library
        will return a 2-tuple, indicating (1) if the graphstack was updated and (2) if it was
        created for the first time.
        """
        if content_client:
            with self.lock:
                data = self.data_for_library(model)
                try:
                    record = self.get_record(model)
                    # this content item exists in ACE, try to update
                    response = content_client.update_content(record.axilent_content_type,
                                                             record.axilent_content_key,
                                                             **data)
                    return (True,False)
                except AxilentContentRecord.DoesNotExist:
                    # new content
                    local_content_type = ContentType.objects.get_for_model(model)
                    axilent_content_type = model.Axilent.content_type
                    response = content_client.create_content(axilent_content_type,
                                                             **data)
                
                    # create new record
                    self.create(local_content_type=local_content_type,
                                local_id=model.pk,
                                axilent_content_type=axilent_content_type,
                                axilent_content_key=response)
            
                    return (True,True)
        else:
            return (False,False)
    
    def search(self,model_class,query):
        """
        Searches ACE and provides model instances that match the search results.
        """
        content_type = model_class.Axilent.content_type
        search_results = content_client.search(query,content_type)
        content_records = self.filter(axilent_content_type=content_type,axilent_content_key__in=[result.key for result in search_results])
        return model_class.objects.filter(pk__in=[record.local_id for record in content_records])

class AxilentContentRecord(models.Model):
    """
    Mapping for a specific Axilent content item to a model.
    """
    local_content_type = models.ForeignKey(ContentType,related_name='axilent_content_records')
    local_id = models.IntegerField()
    axilent_content_type = models.CharField(max_length=100)
    axilent_content_key = models.CharField(max_length=100)
    updated = models.DateTimeField(null=True)
    
    objects = AxilentContentRecordManager()
    
    def update_available(self):
        """
        Determines if a new content update is available from Axilent.
        """
        if content_client.latest_update(self.axilent_content_type,self.axilent_content_key):
            return True
        else:
            return False

    def get_update(self):
        """
        Gets the updated content from Axilent.
        """
        latest = content_client.latest_update(self.axilent_content_type,self.axilent_content_key)
        if self.updated and self.updated >= latest:
            return None
        else:
            return content_client.get_content(self.axilent_content_type,self.axilent_content_key)
    
    def sync_content(self,axilent_content):
        """
        Syncs the local content to the incoming axilent content (a dictionary).
        """
        local_model = self.get_local_model()
        field_map = local_model.Axilent.field_map
        
        log.debug('syncing local model with Axilent content %s, using field map %s.' % (unicode(axilent_content),unicode(field_map)))
        
        # Iterate through the field map and set the local model values from the incoming Axilent content
        deferred_field_converters = []
        for axilent_field, model_field in field_map.items():
            try:            
                if hasattr(model_field,'field'):
                    # this is a field converter
                    # sanity check
                    if not hasattr(model_field,'to_ace') or not hasattr(model_field,'to_local_model'):
                        raise ValueError('You must define the methods to_ace and to_local_model for field converter for ace field %s.' % axilent_field)
                
                    if hasattr(model_field,'deferred') and model_field.deferred:
                        deferred_field_converters.append((axilent_field,model_field))
                    else:
                        value = model_field.to_local_model(axilent_content,getattr(axilent_content,axilent_field))
                        setattr(local_model,model_field.field,value)
                else:
                    # not a field converter, just a string.  Use DefaultFieldConverter
                    default_field_converter = DefaultFieldConverter(model_field)
                    setattr(local_model,model_field,default_field_converter.to_local_model(axilent_content,getattr(axilent_content,axilent_field)))
            except AttributeError:
                log.exception('Local model has no field %s (matched to Axilent field %s).' % (model_field,axilent_field))
        
        local_model.save()
        
        if deferred_field_converters:
            for deferred_axilent_field, deferred_model_field in deferred_field_converters:
                try:
                    deferred_model_field.to_local_model(axilent_content,getattr(axilent_content,axilent_field),local_model)
                except AttributeError:
                    log.exception('Local model has no field %s (matched to Axilent field %s).' % (deferred_model_field.field,deferred_axilent_field))
            
            local_model.save()
        else:
            log.info('No deferred field converters for %s.' % unicode(local_model))
            
            
        self.updated = datetime.now()
        self.save()
        
        return local_model
    
    def get_local_model(self):
        """
        Gets the local model for this record.
        """
        return self.local_content_type.model_class().objects.get(pk=self.local_id)
    
    def archive(self):
        """
        Archives the content on Axilent.
        """
        return library_client.archive_content(library_project,self.axilent_content_type,self.axilent_content_key)
    
    def live_delete(self):
        """
        Deletes the deployed version of this content from ACE.
        """
        return content_client.delete_content(self.axilent_content_type,self.axilent_content_key)
    
    def tag(self,tag_term,update_library_index=True):
        """
        Applies tag term to content.
        """
        return library_client.tag_content(library_project,
                                          self.axilent_content_type,
                                          self.axilent_content_key,
                                          tag_term,
                                          search_index=update_library_index)
    
    def detag(self,tag_term):
        """
        Removes tag from the content.
        """
        return library_client.detag_content(library_project,self.axilent_content_type,self.axilent_content_key,tag_term)
    
    def live_tag(self,tag_term):
        """
        Tags the deployed version of the content in the graphstack.
        """
        return content_client.tag_content(self.axilent_content_type,self.axilent_content_key,tag_term)
    
    def live_detag(self,tag_term):
        """
        De-tags the deployed veresion of the content in the graphstack.
        """
        return content_client.detag_content(self.axilent_content_type,self.axilent_content_key,tag_term)
    
    def reindex(self):
        """
        Re-indexes the deployed content for search.
        """
        return content_client.reindex_content(self.axilent_content_type,self.axilent_content_key)
    
    class Meta:
        unique_together = (('local_content_type','local_id'),('axilent_content_type','axilent_content_key'))

class ContentSyncLock(models.Model):
    """
    Lock for a content sync.  Indicates a sync is under way.
    """
    token = models.CharField(max_length=100)

class ProfileRecordManager(models.Manager):
    """
    Manager for the profile record.
    """
    def for_user(self,user):
        """
        Gets or creates a profile record for the user.  User may not be anonymous for this call.
        """
        if user.is_anonymous():
            raise ValueError('You cannon use an anonymous user for the ProfileRecord.objects.for_user() method.')
        
        try:
            return (self.get(user=user).profile,False)
        except ProfileRecord.DoesNotExist:
            profile = trigger_client.profile()
            record = self.create(user=user,profile=profile)
            return (record.profile,True)
    
    def for_request(self,request):
        """
        Gets or creates a profile from the request object.  Will attempt to use profile record
        registered with a logged in user, failing that will fall back to a cookie.
        
        Returns a tuple of the ProfileRecord and a boolean flag indicating if the record
        was just created.
        """
        if not request.user.is_anonymous():
            return self.for_user(request.user)
        
        profile = request.COOKIES.get('axilent-profile',None)
        profile_record = None
        if profile:
            return self.get_or_create(profile=profile)
        else:
            profile_record = self.create(profile=trigger_client.profile())
            return (profile_record,True)

class ProfileRecord(models.Model):
    """
    A record associating a Django user with an ACE profile.
    """
    user = models.ForeignKey(User,related_name='ace_profile_record',unique=True,null=True)
    profile = models.CharField(max_length=100)
    
    objects = ProfileRecordManager()
    
    def __unicode__(self):
        return self.profile

class AuthTokenManager(models.Manager):
    """
    Manager class for AuthToken.
    """
    def new_token(self,origin_domain=None):
        """
        Creates a new token.
        """
        return self.create(origin_domain=origin_domain,
                           token=uuid.uuid4().hex)

class AuthToken(models.Model):
    """
    Token for remote management of Djax install.
    """
    origin_domain = models.URLField(null=True)
    token = models.CharField(max_length=100,unique=True)
    
    objects = AuthTokenManager()
    
    def __unicode__(self):
        return self.token


# =================
# = Registry Hook =
# =================
build_registry()