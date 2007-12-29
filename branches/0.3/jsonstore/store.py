from urllib import unquote
from urlparse import urljoin
import md5
import datetime
import time

from paste import httpexceptions
from paste.request import parse_dict_querystring, construct_url
from httpencode import parse_request, get_format
from simplejson import dumps, loads

from jsonstore.backends import EntryManager


def make_app(global_conf, dsn, number_of_entries=10, **kwargs):
    """
    Create a JSON Atom store.

    Configuration should be like this::

        [app:jsonstore]
        use = egg:jsonstore
        dsn = driver://user:password@host:port/dbname
        number_of_entries = 10

    """
    noe = int(number_of_entries)
    store = JSONStore(dsn, number_of_entries=noe)
    return store


class JSONStore(object):
    """
    A RESTful store based on JSON.

    """
    def __init__(self, dsn, number_of_entries=10, responder=None):
        self.em = EntryManager(dsn)
        self.number_of_entries = number_of_entries
        self.responder = responder or get_format('json').responder

    def __call__(self, environ, start_response):
        query = parse_dict_querystring(environ)
        method = (query.get('REQUEST_METHOD') or
                environ['REQUEST_METHOD'])
        func = getattr(self, '_%s' % method)
        return func(environ, start_response)

    def _GET(self, environ, start_response):
        # Unserialize PATH_INFO to a JSON object.
        path_info = environ.get('PATH_INFO', '/')
        path_info = unquote(path_info)
        path_info = path_info.strip('/') or 'null'  # use null if path is /
        obj = loads(path_info)
        
        # Single entry.
        if isinstance(obj, (int, long, float, basestring)): 
            try:
                output = self.em.get_entry(obj)
            except (KeyError, TypeError):
                raise httpexceptions.HTTPNotFound()  # 404

            output.setdefault('@namespaces', {})
            output['jsonstore:id'] = output.pop('__id__')
            output['jsonstore:updated'] = output.pop('__updated__')

        # Collection from listing or search.
        else:
            query = parse_dict_querystring(environ)
            size = int(query.get("size", self.number_of_entries))
            offset = int(query.get("offset", 0))

            # Return store listing.
            if obj is None:
                entries = self.em.get_entries(size+1, offset)
            # Return a JSON search.
            else:
                # Convert external identifiers to internal.
                if 'jsonstore:id' in obj: 
                    obj['__id__'] = obj.pop('jsonstore:id')
                if 'jsonstore:updated' in obj:
                    obj['__updated__'] = obj.pop('jsonstore:updated')
                mode = int(query.get("search", 0))
                entries = self.em.search(obj, mode, size+1, offset)

            output = {'@namespaces': {}}

            # Check number of entries for a "next" entry.
            if len(entries) == size+1:
                entries.pop()  # remove "next" entry
                qs = query.copy()
                qs['size'] = str(size)
                qs['offset'] = str(offset+size)
                qs = '&'.join(['%s=%s' % p for p in qs.items()])
                output['jep:next'] = construct_url(environ, querystring=qs)
            else:
                output['jep:next'] = None

            # Change jsonstore identifiers to namespaced variables.
            for entry in entries:
                entry['jsonstore:id'] = entry.pop('__id__')
                entry['jsonstore:updated'] = entry.pop('__updated__')
                if '@namespaces' in entry:
                    ns = entry.pop('@namespaces')
                    output['@namespaces'].update(ns)
                etag = md5.new(dumps(entry)).hexdigest()
                entry['jep:etag'] = etag
            
            baseurl = construct_url(environ,
                    with_query_string=False, with_path_info=False)
            output['jep:members'] = [
                    {'jep:href': '%s/%s' % (baseurl, entry['jsonstore:id']),
                     'jep:entity': entry}
                    for entry in entries]

        # Add namespace to object.
        output['@namespaces']['jsonstore'] = "http://dealmeida.net/projects/jsonstore/"
        output['@namespaces']['jep'] = "http://bitworking.org/news/JEP"

        # Calculate etag.
        etag = md5.new(dumps(output)).hexdigest()
        output['jep:etag'] = etag

        app = self.responder(output,
                content_type='application/json',
                headers=[('Etag', etag)])
        return app(environ, start_response)

    def _HEAD(self, environ, start_response):
        self._GET(environ, start_response)
        return []

    def _POST(self, environ, start_response):
        entry = parse_request(environ, output_type='python')

        # Change __updated__ to a datetime object.
        if 'jsonstore:updated' in entry:
            updated = entry.pop('jsonstore:updated')
            entry['__updated__'] = datetime.datetime(
                    *(time.strptime(updated, '%Y-%m-%dT%H:%M:%SZ')[0:6]))
        if 'jsonstore:id' in entry:
            entry['__id__'] = entry.pop('jsonstore:id')

        # Create the entry.
        output = self.em.create_entry(entry)
        output['jsonstore:id'] = output.pop('__id__')
        output['jsonstore:updated'] = output.pop('__updated__')
        output.setdefault('@namespaces', {})
        output['@namespaces']['jsonstore'] = "http://dealmeida.net/projects/jsonstore/"
        output['@namespaces']['jep'] = "http://bitworking.org/news/JEP"

        # Generate new resource location.
        store = construct_url(environ, with_query_string=False,
                with_path_info=False)
        location = urljoin(store, str(output['jsonstore:id']))
        app = self.responder(output,
                content_type='application/json',
                headers=[('Location', location)])

        # Fake start response to return 201 status.
        def start(status, headers):
            return start_response("201 Created", headers)

        return app(environ, start)

    def _PUT(self, environ, start_response):
        entry = parse_request(environ, output_type='python')

        # Change __updated__ to a datetime object.
        if 'jsonstore:updated' in entry:
            updated = entry.pop('jsonstore:updated')
            entry['__updated__'] = datetime.datetime(
                    *(time.strptime(updated, '%Y-%m-%dT%H:%M:%SZ')[0:6]))

        path_info = environ.get('PATH_INFO', '/')
        path_info = path_info.strip('/')
        id_ = unquote(path_info)

        if 'jsonstore:id' in entry:
            entry['__id__'] = entry.pop('jsonstore:id')
            if id_ != entry['__id__']: raise httpexceptions.HTTPConflict()
        else:
            entry['__id__'] = id_

        # Update entry.
        output = self.em.update_entry(entry)
        output['jsonstore:id'] = output.pop('__id__')
        output['jsonstore:updated'] = output.pop('__updated__')
        output.setdefault('@namespaces', {})
        output['@namespaces']['jsonstore'] = "http://dealmeida.net/projects/jsonstore/"
        output['@namespaces']['jep'] = "http://bitworking.org/news/JEP"

        # Calculate etag.
        etag = md5.new(dumps(output)).hexdigest()
        output['jep:etag'] = etag

        app = self.responder(output,
                content_type='application/json',
                headers=[('Etag', etag)])
        return app(environ, start_response)

    def _DELETE(self, environ, start_response):
        path_info = environ.get('PATH_INFO', '/')
        path_info = path_info.strip('/')
        id_ = unquote(path_info)

        self.em.delete_entry(id_)

        app = self.responder(None, content_type='application/json')
        return app(environ, start_response)

    def close(self):
        self.em.close()
