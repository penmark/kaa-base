# -*- coding: iso-8859-1 -*-
# -----------------------------------------------------------------------------
# db.py - db abstraction module
# -----------------------------------------------------------------------------
# $Id$
#
# -----------------------------------------------------------------------------
# Copyright (C) 2006 Dirk Meyer, Jason Tackaberry
#
# First Edition: Jason Tackaberry <tack@urandom.ca>
# Maintainer:    Jason Tackaberry <tack@urandom.ca>
#
# Please see the file AUTHORS for a complete list of authors.
#
# This library is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version
# 2.1 as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301 USA
#
# -----------------------------------------------------------------------------

__all__ = ['Database', 'QExpr', 'ATTR_SIMPLE', 'ATTR_SEARCHABLE',
           'ATTR_IGNORE_CASE', 'ATTR_INDEXED', 'ATTR_INDEXED_IGNORE_CASE',
           'ATTR_KEYWORDS']

# python imports
import os
import time
import re
import math
import cPickle
import copy_reg
import _weakref
from sets import Set
from pysqlite2 import dbapi2 as sqlite

# kaa base imports
from strutils import str_to_unicode
from _objectrow import ObjectRow

if sqlite.version < '2.1.0':
    raise ImportError('pysqlite 2.1.0 or higher required')
if sqlite.sqlite_version < '3.3.0':
    raise ImportError('sqlite 3.3.0 or higher required')


SCHEMA_VERSION = 0.1
SCHEMA_VERSION_COMPATIBLE = 0.1
CREATE_SCHEMA = """
    CREATE TABLE meta (
        attr        TEXT UNIQUE,
        value       TEXT
    );
    INSERT INTO meta VALUES('keywords_objectcount', 0);
    INSERT INTO meta VALUES('version', %s);

    CREATE TABLE types (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT UNIQUE,
        attrs_pickle    BLOB,
        idx_pickle      BLOB
    );

    CREATE TABLE words (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        word            TEXT,
        count           INTEGER
    );
    CREATE UNIQUE INDEX words_idx on WORDS (word);

    CREATE TABLE words_map (
        rank            INTEGER,
        word_id         INTEGER,
        object_type     INTEGER,
        object_id       INTEGER,
        frequency       FLOAT
    );
    CREATE INDEX words_map_word_idx ON words_map (word_id, rank, object_type, object_id);
    CREATE INDEX words_map_object_idx ON words_map (object_id, object_type);
    CREATE TRIGGER delete_words_map DELETE ON words_map
    BEGIN
        UPDATE words SET count=count-1 WHERE id=old.word_id;
    END;
"""


ATTR_SIMPLE              = 0x00
ATTR_SEARCHABLE          = 0x01      # Is a SQL column, not a pickled field
ATTR_INDEXED             = 0x02      # Will have an SQL index
ATTR_KEYWORDS            = 0x04      # Indexed for keyword queries
ATTR_IGNORE_CASE         = 0x08      # Store in db as lowercase for searches.
ATTR_INDEXED_IGNORE_CASE = ATTR_INDEXED | ATTR_IGNORE_CASE

STOP_WORDS = (
    "about", "and", "are", "but", "com", "for", "from", "how", "not",
    "some", "that", "the", "this", "was", "what", "when", "where", "who",
    "will", "with", "the", "www", "http", "org", "of", "on"
)
WORDS_DELIM = re.compile("[\W_\d]+", re.U)

# Word length limits for keyword indexing
MIN_WORD_LENGTH = 2
MAX_WORD_LENGTH = 30

# These are special attributes for querying.  Attributes with
# these names cannot be registered.
RESERVED_ATTRIBUTES = ("parent", "object", "keywords", "type", "limit",
                       "attrs", "distinct")


def _list_to_printable(value):
    """
    Takes a list of mixed types and outputs a unicode string.  For
    example, a list [42, 'foo', None, "foo's' string"], this returns the
    string:

        (42, 'foo', NULL, 'foo''s'' string')

    Single quotes are escaped as ''.  This is suitable for use in SQL
    queries.
    """
    fixed_items = []
    for item in value:
        if type(item) in (int, long, float):
           fixed_items.append(str(item))
        elif item == None:
            fixed_items.append("NULL")
        elif type(item) == unicode:
            fixed_items.append("'%s'" % item.replace("'", "''"))
        elif type(item) == str:
            fixed_items.append("'%s'" % str_to_unicode(item.replace("'", "''")))
        else:
            raise Exception, "Unsupported type '%s' given to _list_to_printable" % type(item)

    return '(' + ','.join(fixed_items) + ')'



class QExpr(object):
    """
    Flexible query expressions for use with Database.query()
    """
    def __init__(self, operator, operand):
        operator = operator.lower()
        assert(operator in ("=", "!=", "<", "<=", ">", ">=", "in", "not in", "range", "like"))
        if operator in ("in", "not in", "range"):
            assert(isinstance(operand, (list, tuple)))
            if operator == "range":
                assert(len(operand) == 2)

        self._operator = operator
        self._operand = operand

    def as_sql(self, var):
        if self._operator == "range":
            a, b = self._operand
            return "%s >= ? AND %s <= ?" % (var, var), (a, b)
        elif self._operator in ("in", "not in"):
            return "%s %s %s" % (var, self._operator.upper(),
                   _list_to_printable(self._operand)), ()
        else:
            return "%s %s ?" % (var, self._operator.upper()), \
                   (self._operand,)


# Register handlers for pickling ObjectRow objects.

def _pickle_ObjectRow(o):
    return _unpickle_ObjectRow, (o.items(),)

def _unpickle_ObjectRow(items):
    return ObjectRow(None, None, dict(items))

copy_reg.pickle(ObjectRow, _pickle_ObjectRow, _unpickle_ObjectRow)


class Database:
    def __init__(self, dbfile = None):
        if not dbfile:
            dbfile = "kaa.db.sqlite"

        self._object_types = {}
        self._dbfile = dbfile
        self._open_db()


    def __del__(self):
        self.commit()


    def _open_db(self):
        self._db = sqlite.connect(self._dbfile)
        self._cursor = self._db.cursor()
        self._cursor.execute("PRAGMA synchronous=OFF")
        #self._cursor.execute("PRAGMA temp_store=MEMORY")
        self._cursor.execute("PRAGMA count_changes=OFF")
        self._cursor.execute("PRAGMA cache_size=50000")

        class Cursor(sqlite.Cursor):
            _db = _weakref.ref(self)
        self._db.row_factory = ObjectRow
        # Queries done through this cursor will use the ObjectRow row factory.
        self._qcursor = self._db.cursor(Cursor)

        if not self.check_table_exists("meta"):
            self._db.executescript(CREATE_SCHEMA % SCHEMA_VERSION)

        row = self._db_query_row("SELECT value FROM meta WHERE attr='version'")
        if float(row[0]) < SCHEMA_VERSION_COMPATIBLE:
            raise SystemError, "Database '%s' has schema version %s; required %s" % \
                               (self._dbfile, row[0], SCHEMA_VERSION_COMPATIBLE)

        self._load_object_types()


    def _db_query(self, statement, args = (), cursor = None):
        if not cursor:
            cursor = self._cursor
        cursor.execute(statement, args)
        rows = cursor.fetchall()
        #print "QUERY (%d): %s" % (len(rows), statement)
        return rows


    def _db_query_row(self, statement, args = (), cursor = None):
        rows = self._db_query(statement, args, cursor)
        if len(rows) == 0:
            return None
        return rows[0]


    def check_table_exists(self, table):
        res = self._db_query_row("SELECT name FROM sqlite_master where " \
                                 "name=? and type='table'", (table,))
        return res != None


    def _register_check_indexes(self, indexes, attrs):
        for cols in indexes:
            if type(cols) not in (list, tuple):
                raise ValueError, "Single column index specified ('%s') where multi-column index expected." % cols
            for col in cols:
                errstr = "Multi-column index (%s) contains" % ",".join(cols)
                if col not in attrs:
                    raise ValueError, "%s unknown attribute '%s'" % (errstr, col)
                if not attrs[col][1]:
                    raise ValueError, "%s ATTR_SIMPLE attribute '%s'" % (errstr, col)


    def _register_create_multi_indexes(self, indexes, table_name):
        for cols in indexes:
            self._db_query("CREATE INDEX %s_%s_idx ON %s (%s)" % \
                           (table_name, "_".join(cols), table_name, ",".join(cols)))


    def register_object_type_attrs(self, type_name, indexes = [], **attrs):
        if len(indexes) == len(attrs) == 0:
            raise ValueError, "Must specify indexes or attributes for object type"

        table_name = "objects_%s" % type_name
        if type_name in self._object_types:
            # This type already exists.  Compare given attributes with
            # existing attributes for this type.
            cur_type_id, cur_type_attrs, cur_type_idx = self._object_types[type_name]
            new_attrs = {}
            table_needs_rebuild = False
            changed = False
            for attr_name, (attr_type, attr_flags) in attrs.items():
                if attr_name not in cur_type_attrs or cur_type_attrs[attr_name] != (attr_type, attr_flags):
                    new_attrs[attr_name] = attr_type, attr_flags
                    changed = True
                    if attr_flags:
                        # New attribute isn't simple, needs to alter table.
                        table_needs_rebuild = True

            if not changed:
                return

            # Update the attr list to merge both existing and new attributes.
            attrs = cur_type_attrs.copy()
            attrs.update(new_attrs)
            new_indexes = Set(indexes).difference(cur_type_idx)
            indexes = Set(indexes).union(cur_type_idx)
            self._register_check_indexes(indexes, attrs)

            if not table_needs_rebuild:
                # Only simple (i.e. pickled only) attributes are being added,
                # or only new indexes are added, so we don't need to rebuild the
                # table.
                if len(new_attrs):
                    self._db_query("UPDATE types SET attrs_pickle=? WHERE id=?",
                                   (buffer(cPickle.dumps(attrs, 2)), cur_type_id))

                if len(new_indexes):
                    self._register_create_multi_indexes(new_indexes, table_name)
                    self._db_query("UPDATE types SET idx_pickle=? WHERE id=?",
                                   (buffer(cPickle.dumps(indexes, 2)), cur_type_id))

                self.commit()
                self._load_object_types()
                return

            # We need to update the database now ...

        else:
            # New type definition.
            new_attrs = cur_type_id = None
            # Merge standard attributes with user attributes for this new type.
            attrs.update({
                "id": (int, ATTR_SEARCHABLE),
                "parent_type": (int, ATTR_SEARCHABLE),
                "parent_id": (int, ATTR_SEARCHABLE),
                "pickle": (buffer, ATTR_SEARCHABLE)
            })
            self._register_check_indexes(indexes, attrs)

        create_stmt = "CREATE TABLE %s_tmp ("% table_name

        # Iterate through type attributes and append to SQL create statement.
        sql_types = {int: "INTEGER", float: "FLOAT", buffer: "BLOB",
                     unicode: "TEXT", str: "BLOB"}
        for attr_name, (attr_type, attr_flags) in attrs.items():
            assert(attr_name not in RESERVED_ATTRIBUTES)
            # If flags is non-zero it means this attribute needs to be a
            # column in the table, not a pickled value.
            if attr_flags:
                if attr_type not in sql_types:
                    raise ValueError, "Type '%s' not supported" % str(attr_type)
                create_stmt += "%s %s" % (attr_name, sql_types[attr_type])
                if attr_name == "id":
                    # Special case, these are auto-incrementing primary keys
                    create_stmt += " PRIMARY KEY AUTOINCREMENT"
                create_stmt += ","

        create_stmt = create_stmt.rstrip(",") + ")"
        self._db_query(create_stmt)


        # Add this type to the types table, including the attributes
        # dictionary.
        self._db_query("INSERT OR REPLACE INTO types VALUES(?, ?, ?, ?)",
                       (cur_type_id, type_name, buffer(cPickle.dumps(attrs, 2)),
                        buffer(cPickle.dumps(indexes, 2))))
        self._load_object_types()

        if new_attrs:
            # Migrate rows from old table to new one.
            # FIXME: this does not migrate data in the case of an attribute
            # being changed from simple to searchable or vice versa.  In the
            # simple->searchable case, the data will stay in the pickle; in
            # the searchable->simple case, the data will be lost.
            columns = filter(lambda x: cur_type_attrs[x][1], cur_type_attrs.keys())
            columns = ",".join(columns)
            self._db_query("INSERT INTO %s_tmp (%s) SELECT %s FROM %s" % \
                           (table_name, columns, columns, table_name))

            # Delete old table.
            self._db_query("DROP TABLE %s" % table_name)

        # Rename temporary table.
        self._db_query("ALTER TABLE %s_tmp RENAME TO %s" % \
                       (table_name, table_name))

        # Create a trigger that reduces meta.keywords_objectcount when a row
        # is deleted.
        if self._type_has_keyword_attr(type_name):
            self._db_query("CREATE TRIGGER delete_object_%s DELETE ON %s BEGIN "
                           "UPDATE meta SET value=value-1 WHERE attr='keywords_objectcount'; END" % \
                           (type_name, table_name))

        # Create index for locating all objects under a given parent.
        self._db_query("CREATE INDEX %s_parent_idx on %s (parent_id, "\
                       "parent_type)" % (table_name, table_name))

        # If any of these attributes need to be indexed, create the index
        # for that column.
        for attr_name, (attr_type, attr_flags) in attrs.items():
            if attr_flags & ATTR_INDEXED:
                self._db_query("CREATE INDEX %s_%s_idx ON %s (%s)" % \
                               (table_name, attr_name, table_name, attr_name))

        # Create multi-column indexes; indexes value has already been verified.
        self._register_create_multi_indexes(indexes, table_name)
        self.commit()


    def _load_object_types(self):
        for id, name, attrs, idx in self._db_query("SELECT * from types"):
            self._object_types[name] = id, cPickle.loads(str(attrs)), cPickle.loads(str(idx))

    def _type_has_keyword_attr(self, type_name):
        if type_name not in self._object_types:
            return False
        type_attrs = self._object_types[type_name][1]
        for name, (attr_type, flags) in type_attrs.items():
            if flags & ATTR_KEYWORDS:
                return True
        return False

    def _get_type_attrs(self, type_name):
        return self._object_types[type_name][1]

    def _get_type_id(self, type_name):
        return self._object_types[type_name][0]


    def _make_query_from_attrs(self, query_type, attrs, type_name):
        type_attrs = self._get_type_attrs(type_name)

        columns = []
        values = []
        placeholders = []

        for key in attrs.keys():
            if attrs[key] == None:
                del attrs[key]
        attrs_copy = attrs.copy()
        for name, (attr_type, flags) in type_attrs.items():
            if flags != ATTR_SIMPLE and name in attrs:
                columns.append(name)
                placeholders.append("?")
                value = attrs[name]
                # Coercion for numberic types
                if isinstance(value, (int, long, float)) and attr_type in (int, long, float):
                    value = attr_type(value)
                elif isinstance(value, basestring) and \
                     flags & ATTR_INDEXED_IGNORE_CASE == ATTR_INDEXED_IGNORE_CASE:
                    # If the attribute is ATTR_INDEXED_IGNORE_CASE and it's a string
                    # then we store it as lowercase in the table column, while
                    # keeping the original (unchanged case) value in the pickle.
                    # This allows us to do case-insensitive searches on indexed
                    # columns and still benefit from the index.
                    attrs_copy["__" + name] = value
                    value = value.lower()

                if attr_type != type(value):
                    raise ValueError, "Type mismatch in query for %s: '%s' (%s) is not a %s" % \
                                          (name, str(value), str(type(value)), str(attr_type))
                if attr_type == str:
                    # Treat strings (non-unicode) as buffers.
                    value = buffer(value)
                values.append(value)
                del attrs_copy[name]

        if len(attrs_copy) > 0:
            columns.append("pickle")
            values.append(buffer(cPickle.dumps(attrs_copy, 2)))
            placeholders.append("?")

        table_name = "objects_" + type_name

        if query_type == "add":
            columns = ",".join(columns)
            placeholders = ",".join(placeholders)
            q = "INSERT INTO %s (%s) VALUES(%s)" % (table_name, columns, placeholders)
        else:
            q = "UPDATE %s SET " % table_name
            for col, ph in zip(columns, placeholders):
                q += "%s=%s," % (col, ph)
            # Trim off last comma
            q = q.rstrip(",")
            q += " WHERE id=?"
            values.append(attrs["id"])

        return q, values


    def delete_object(self, (object_type, object_id)):
        """
        Deletes the specified object.
        """
        return self._delete_multiple_objects({object_type: (object_id,)})



    def delete_by_query(self, **attrs):
        """
        Deletes all objects returned by the given query.  See query()
        for argument details.  Returns number of objects deleted.
        """
        attrs["attrs"] = ["id"]
        results = self.query(**attrs)
        if len(results) == 0:
            return 0

        results_by_type = {}
        for o in results:
            if o["type"] not in results_by_type:
                results_by_type[o["type"]] = []
            results_by_type[o["type"]].append(o["id"])

        return self._delete_multiple_objects(results_by_type)


    def _delete_multiple_objects(self, objects):
        self._delete_multiple_objects_keywords(objects)
        child_objects = {}
        count = 0
        for object_type, object_ids in objects.items():
            object_type_id = self._get_type_id(object_type)
            if len(object_ids) == 0:
                continue

            object_ids_str = _list_to_printable(object_ids)
            self._db_query("DELETE FROM objects_%s WHERE id IN %s" % \
                           (object_type, object_ids_str))
            count += self._cursor.rowcount

            # Record all children of this object so we can later delete them.
            for tp_name, (tp_id, tp_attrs, tp_idx) in self._object_types.items():
                children_ids = self._db_query("SELECT id FROM objects_%s WHERE parent_id IN %s AND parent_type=?" % \
                                              (tp_name, object_ids_str), (object_type_id,))
                if len(children_ids):
                    child_objects[tp_name] = [x[0] for x in children_ids]

        if len(child_objects):
            # If there are any child objects of the objects we just deleted,
            # delete those now.
            count += self._delete_multiple_objects(child_objects)

        return count


    def add_object(self, object_type, parent = None, **attrs):
        """
        Adds an object of type 'object_type' to the database.  Parent is a
        (type, id) tuple which refers to the object's parent.  'object_type'
        and 'type' is a type name as given to register_object_type_attrs().
        attrs kwargs will vary based on object type.  ATTR_SIMPLE attributes
        which a None are not added.

        This method returns the dict that would be returned if this object
        were queried by query().  The "id" key of this dict refers
        to the id number assigned to this object.
        """
        type_attrs = self._get_type_attrs(object_type)
        if parent:
            attrs["parent_type"] = self._get_type_id(parent[0])
            attrs["parent_id"] = parent[1]

        query, values = self._make_query_from_attrs("add", attrs, object_type)
        self._db_query(query, values)

        # Add id given by db, as well as object type.
        attrs["id"] = self._cursor.lastrowid
        attrs["type"] = object_type

        # Index keyword attributes
        word_parts = []
        for name, (attr_type, flags) in type_attrs.items():
            if name in attrs and flags & ATTR_KEYWORDS:
                word_parts.append((attrs[name], 1.0, attr_type, flags))
        words = self._score_words(word_parts)
        self._add_object_keywords((object_type, attrs["id"]), words)

        if self._type_has_keyword_attr(object_type):
            self._db_query("UPDATE meta SET value=value+1 WHERE attr='keywords_objectcount'")

        class DummyCursor:
            _db = _weakref.ref(self)
            # List of non ATTR_SIMPLE attributes for this object type.
            description = [None] + [ (x[0],) for x in type_attrs.items() if x[1][1] != 0 ]

        # Create a row that matches the description order.
        row = [object_type] + [ attrs.pop(x[0], None) for x in DummyCursor.description[1:] ]
        # Return the ObjectRow for this object.
        return ObjectRow(DummyCursor(), row, attrs)


    def update_object(self, (object_type, object_id), parent = None, **attrs):
        """
        Update an object in the database.  For updating, object is identified
        by a (type, id) tuple.  Parent is a (type, id) tuple which refers to
        the object's parent.  If specified, the object is reparented,
        otherwise the parent remains the same as when it was added with
        add_object().  attrs kwargs will vary based on object type.  If a
        ATTR_SIMPLE attribute is set to None, it will be removed from the
        pickled dictionary.
        """
        type_attrs = self._get_type_attrs(object_type)

        # Figure out whether we need to reindex keywords (i.e. ATTR_KEYWORDS
        # attribute is specified in the kwargs), and what ATTR_KEYWORDS
        # attributes exist for this object.
        needs_keyword_reindex = False
        keyword_columns = []
        for name, (attr_type, flags) in type_attrs.items():
            if flags & ATTR_KEYWORDS:
                if name in attrs:
                    needs_keyword_reindex = True
                keyword_columns.append(name)

        # Get the pickle for this object, as well as all keyword attributes
        # if we need a keyword reindex.  First construct the query.
        # FIXME: we don't need to get the pickle if no ATTR_SIMPLE attrs
        # are being updated.
        q = "SELECT pickle%%s FROM objects_%s WHERE id=?" % object_type
        if needs_keyword_reindex:
            q %= "," + ",".join(keyword_columns)
        else:
            q %= ""
        # Now get the row.
        row = self._db_query_row(q, (object_id,))
        # TODO: raise a more useful exception here.
        assert(row)
        if row[0]:
            row_attrs = cPickle.loads(str(row[0]))
            row_attrs.update(attrs)
            attrs = row_attrs
        if parent:
            attrs["parent_type"] = self._get_type_id(parent[0])
            attrs["parent_id"] = parent[1]
        attrs["id"] = object_id
        query, values = self._make_query_from_attrs("update", attrs, object_type)
        self._db_query(query, values)

        if needs_keyword_reindex:
            # We've modified a ATTR_KEYWORD column, so we need to reindex all
            # all keyword attributes for this row.

            # Merge the other keyword columns into attrs dict.
            for n, name in zip(range(len(keyword_columns)), keyword_columns):
                if name not in attrs:
                    attrs[name] = row[n + 1]

            # Remove existing indexed words for this object.
            self._delete_object_keywords((object_type, object_id))

            # Re-index
            word_parts = []
            for name, (attr_type, flags) in type_attrs.items():
                if flags & ATTR_KEYWORDS:
                    if attr_type == str and type(attrs[name]) == buffer:
                        # _score_words wants only string or unicode values.
                        attrs[name] = str(attrs[name])
                    word_parts.append((attrs[name], 1.0, attr_type, flags))
            words = self._score_words(word_parts)
            self._add_object_keywords((object_type, object_id), words)


    def commit(self):
        self._db.commit()


    def query(self, **attrs):
        """
        Query the database for objects matching all of the given attributes
        (specified in kwargs).  There are a few special kwarg attributes:

             parent: (type, id) tuple referring to the object's parent, where
                     type is the name of the type and id is the database id
                     of the parent, or a QExpr.   parent may also be a tuple
                     of (type, id) tuples.
             object: (type, id) tuple referring to the object itself.
           keywords: a string of search terms for keyword search.
               type: only search items of this type (e.g. "images"); if None
                     (or not specified) all types are searched.
              limit: return only this number of results; if None (or not
                     specified) all matches are returned.  For better
                     performance it is highly recommended a limit is specified
                     for keyword searches.
              attrs: A list of attributes to be returned.  If not specified,
                     all possible attributes.
           distinct: If True, selects only distinct rows.  When distinct is
                     specified, attrs kwarg must also be given, and no
                     specified attrs can be ATTR_SIMPLE.

        Return value is a list of ObjectRow objects, which behave like
        dictionaries in most respects.  Attributes defined in the object
        type are accessible, as well as 'type' and 'parent' keys.
        """
        query_info = {}
        parents = []
        query_type = "ALL"
        results = []
        query_info["columns"] = {}
        query_info["attrs"] = {}

        if "object" in attrs:
            attrs["type"], attrs["id"] = attrs["object"]
            del attrs["object"]

        if "keywords" in attrs:
            # TODO: Possible optimization: do keyword search after the query
            # below only on types that have results iff all queried columns are
            # indexed.

            # If search criteria other than keywords are specified, we can't
            # enforce a limit on the keyword search, otherwise we might miss
            # intersections.
            if len(Set(attrs).difference(("type", "limit", "keywords"))) > 0:
                limit = None
            else:
                limit = attrs.get("limit")
            kw_results = self._query_keywords(attrs["keywords"], limit,
                                              attrs.get("type"))

            # No matches to our keyword search, so we're done.
            if not kw_results:
                return []

            kw_results_by_type = {}
            for tp, id in kw_results:
                if tp not in kw_results_by_type:
                    kw_results_by_type[tp] = []
                kw_results_by_type[tp].append(id)

            del attrs["keywords"]
        else:
            kw_results = kw_results_by_type = None


        if "type" in attrs:
            if attrs["type"] not in self._object_types:
                raise ValueError, "Unknown object type '%s'" % attrs["type"]
            type_list = [(attrs["type"], self._object_types[attrs["type"]])]
            del attrs["type"]
        else:
            type_list = self._object_types.items()

        if "parent" in attrs:
            # ("type", id_or_QExpr) or (("type1", id_or_QExpr), ("type2", id_or_QExpr), ...)
            if type(attrs["parent"][0]) != tuple:
                # Convert first form to second form.
                attrs["parent"] = (attrs["parent"],)

            for parent_type_name, parent_id in attrs["parent"]:
                parent_type_id = self._get_type_id(parent_type_name)
                if type(parent_id) != QExpr:
                    parent_id = QExpr("=", parent_id)
                parents.append((parent_type_id, parent_id))
            del attrs["parent"]

        if "limit" in attrs:
            result_limit = attrs["limit"]
            del attrs["limit"]
        else:
            result_limit = None

        if "attrs" in attrs:
            requested_columns = attrs["attrs"]
            del attrs["attrs"]
        else:
            requested_columns = None

        if "distinct" in attrs:
            if attrs["distinct"]:
                if not requested_columns:
                    raise ValueError, "Distinct query specified, but no attrs kwarg given."
                query_type = "DISTINCT"
            del attrs["distinct"]


        for type_name, (type_id, type_attrs, type_idx) in type_list:
            if kw_results and type_id not in kw_results_by_type:
                # If we've done a keyword search, don't bother querying
                # object types for which there were no keyword hits.
                continue

            # Select only sql columns (i.e. attrs that aren't ATTR_SIMPLE)
            all_columns = filter(lambda x: type_attrs[x][1] != ATTR_SIMPLE, type_attrs.keys())
            if requested_columns:
                columns = requested_columns
                # Ensure that all the requested columns exist for this type
                missing = tuple(Set(columns).difference(type_attrs.keys()))
                if missing:
                    raise ValueError, "One or more requested attributes %s are not available for type '%s'" % \
                                      (str(missing), type_name)
                # Ensure that no requested attrs are ATTR_SIMPLE
                simple = [ x for x in columns if type_attrs[x][1] == ATTR_SIMPLE ]
                if simple:
                    raise ValueError, "ATTR_SIMPLE attributes cannot yet be specified in attrs kwarg %s" % \
                                      str(tuple(simple))
            else:
                columns = all_columns

            # Construct a query based on the supplied attributes for this
            # object type.  If any of the attribute names aren't valid for
            # this type, then we don't bother matching, since this an AND
            # query and there aren't be any matches.
            if len(Set(attrs).difference(all_columns)) > 0:
                continue

            q = []
            query_values = []
            q.append("SELECT %s '%s'%%s,%s FROM objects_%s" % \
                (query_type, type_name, ",".join(columns), type_name))

            if kw_results != None:
                q[0] %= ",%d+id as computed_id" % (type_id * 10000000)
                q.append("WHERE")
                q.append("id IN %s" % _list_to_printable(kw_results_by_type[type_id]))
            else:
                q[0] %= ""

            if len(parents):
                q.append(("WHERE", "AND")["WHERE" in q])
                expr = []
                for parent_type, parent_id in parents:
                    sql, values = parent_id.as_sql("parent_id")
                    expr.append("(parent_type=? AND %s)" % sql)
                    query_values += (parent_type,) + values
                q.append("(%s)" % " OR ".join(expr))

            for attr, value in attrs.items():
                attr_type = type_attrs[attr][0]
                if type(value) != QExpr:
                    value = QExpr("=", value)

                # Coerce between numeric types; also coerce a string of digits into a numeric
                # type.
                if attr_type in (int, long, float) and (type(value._operand) in (int, long, float) or \
                    isinstance(value._operand, basestring) and value._operand.isdigit()):
                    value._operand = attr_type(value._operand)

                # Verify expression operand type is correct for this attribute.
                if value._operator not in ("range", "in", "not in") and \
                   type(value._operand) != attr_type:
                    raise ValueError, "Type mismatch in query: '%s' (%s) is not a %s" % \
                                          (str(value._operand), str(type(value._operand)), str(attr_type))

                # Queries on string columns are case-insensitive.
                if isinstance(value._operand, basestring) and type_attrs[attr][1] & ATTR_IGNORE_CASE:
                    value._operand = value._operand.lower()
                    if not (type_attrs[attr][1] & ATTR_INDEXED):
                        # If this column is ATTR_INDEXED then we already ensure
                        # the values are stored in lowercase in the db, so we
                        # don't want to get sql to lower() the column because
                        # it's needless, and more importantly, we won't be able
                        # to use any indices on the column.
                        attr = 'lower(%s)' % attr

                if type(value._operand) == str:
                    # Treat strings (non-unicode) as buffers.
                    value._operand = buffer(value._operand)

                q.append(("WHERE", "AND")["WHERE" in q])

                sql, values = value.as_sql(attr)
                q.append(sql)
                query_values.extend(values)

            if result_limit != None:
                q.append(" LIMIT %d" % result_limit)

            q = " ".join(q)
            rows = self._db_query(q, query_values, cursor = self._qcursor)

            if result_limit != None:
                results.extend(rows[:result_limit - len(results) + 1])
            else:
                results.extend(rows)

            if kw_results:
                query_info["columns"][type_name] = ["type", "computed_id"] + columns
            else:
                query_info["columns"][type_name] = ["type"] + columns
            query_info["attrs"][type_name] = type_attrs

            if result_limit != None and len(rows) == result_limit:
                # No need to try the other types, we're done.
                break

        # If keyword search was done, sort results to preserve order given in
        # kw_results.
        if kw_results:
            # Convert (type,id) tuple to computed id value.
            kw_results = map(lambda (type, id): type*10000000+id, kw_results)
            # Create a dict mapping each computed id value to its position.
            kw_results_order = dict(zip(kw_results, range(len(kw_results))))
            # Now sort based on the order dict.  The second item in each row
            # will be the computed id for that row.
            results.sort(lambda a, b: cmp(kw_results_order[a[1]], kw_results_order[b[1]]))

        return results



    def _score_words(self, text_parts):
        """
        Scores the words given in text_parts, which is a list of tuples
        (text, coeff, type), where text is the string of words
        to be scored, coeff is the weight to give each word in this part
        (1.0 is normal), and type is one of ATTR_KEYWORDS_*.  Text parts are
        either unicode objects or strings.  If they are strings, they are
        given to str_to_unicode() to try to decode them intelligently.

        Each word W is given the score:
             sqrt( (W coeff * W count) / total word count )

        Counts are relative to the given object, not all objects in the
        database.

        Returns a dict of words whose values hold the score caclulated as
        above.
        """
        words = {}
        total_words = 0

        for text, coeff, attr_type, flags in text_parts:
            if not text:
                continue
            if type(text) not in (unicode, str):
                raise ValueError, "Invalid type (%s) for ATTR_KEYWORDS attribute.  Only unicode or str allowed." % \
                                  str(type(text))
            if attr_type == str:
                text = str_to_unicode(text)

            # FIXME: don't hardcode path length; is there a PATH_MAX in python?
            if len(text) < 255 and re.search("\.\w{2,5}$", text):
                # Treat input as filename since it looks like it ends with an extension.
                dirname, filename = os.path.split(text)
                fname_noext, ext = os.path.splitext(filename)
                # Remove the first 2 levels (like /home/user/) and then take
                # the last two levels that are left.
                levels = dirname.strip('/').split(os.path.sep)[2:][-2:] + [fname_noext]
                parsed = WORDS_DELIM.split(' '.join(levels)) + [fname_noext]
            else:
                parsed = WORDS_DELIM.split(text)

            for word in parsed:
                if not word or len(word) > MAX_WORD_LENGTH:
                    # Probably not a word.
                    continue
                word = word.lower()

                if len(word) < MIN_WORD_LENGTH or word in STOP_WORDS:
                    continue
                if word not in words:
                    words[word] = coeff
                else:
                    words[word] += coeff
                total_words += 1

        # Score based on word frequency in document.  (Add weight for
        # non-dictionary words?  Or longer words?)
        for word, score in words.items():
            words[word] = math.sqrt(words[word] / total_words)
        return words


    def _delete_object_keywords(self, (object_type, object_id)):
        """
        Removes all indexed keywords for the given object.  This function
        must be called when an object is removed from the database, or when
        an object is being updated (and therefore its keywords must be
        re-indexed).
        """
        self._delete_multiple_objects_keywords({object_type: (object_id,)})


    def _delete_multiple_objects_keywords(self, objects):
        """
        objects = dict type_name -> ids tuple
        """
        count = 0
        for type_name, object_ids in objects.items():
            # Resolve object type name to id
            type_id = self._get_type_id(type_name)

            # Remove all words associated with this object.  A trigger will
            # decrement the count column in the words table for all word_id
            # that get affected.
            self._db_query("DELETE FROM words_map WHERE object_type=? AND object_id IN %s" % \
                           _list_to_printable(object_ids), (type_id,))
            count += self._cursor.rowcount



    def _add_object_keywords(self, (object_type, object_id), words):
        """
        Adds the dictionary of words (as computed by _score_words()) to the
        database for the given object.
        """
        # Resolve object type name to id
        object_type = self._get_type_id(object_type)

        # Holds any of the given words that already exist in the database
        # with their id and count.
        db_words_count = {}

        words_list = _list_to_printable(words.keys())
        q = "SELECT id,word,count FROM words WHERE word IN %s" % words_list
        rows = self._db_query(q)
        for row in rows:
            db_words_count[row[1]] = row[0], row[2]

        # For executemany queries later.
        update_list, map_list = [], []

        for word, score in words.items():
            if word not in db_words_count:
                # New word, so insert it now.
                self._db_query("INSERT OR REPLACE INTO words VALUES(NULL, ?, 1)", (word,))
                db_id, db_count = self._cursor.lastrowid, 1
                db_words_count[word] = db_id, db_count
            else:
                db_id, db_count = db_words_count[word]
                update_list.append((db_count + 1, db_id))

            map_list.append((int(score*10), db_id, object_type, object_id, score))

        self._cursor.executemany("UPDATE words SET count=? WHERE id=?", update_list)
        self._cursor.executemany("INSERT INTO words_map VALUES(?, ?, ?, ?, ?)", map_list)


    def _query_keywords(self, words, limit = 100, object_type = None):
        """
        Queries the database for the keywords supplied in the words strings.
        (Search terms are delimited by spaces.)

        The search algorithm tries to optimize for the common case.  When
        words are scored (_score_words()), each word is assigned a score that
        is stored in the database (as a float) and also as an integer in the
        range 0-10, called rank.  (So a word with score 0.35 has a rank 3.)

        Multiple passes are made over the words_map table, first starting at
        the highest rank fetching a certain number of rows, and progressively
        drilling down to lower ranks, trying to find enough results to fill our
        limit that intersects on all supplied words.  If our limit isn't met
        and all ranks have been searched but there are still more possible
        matches (because we use LIMIT on the SQL statement), we expand the
        LIMIT (currently by an order of 10) and try again, specifying an
        OFFSET in the query.

        The worst case scenario is given two search terms, each term matches
        50% of all rows but there is only one intersection row.  (Or, more
        generally, given N terms, each term matches (1/N)*100 percent rows with
        only 1 row intersection between all N terms.)   This could be improved
        by avoiding the OFFSET/LIMIT technique as described above, but that
        approach provides a big performance win in more common cases.  This
        case can be mitigated by caching common word combinations, but it is
        an extremely difficult problem to solve.

        object_type specifies an type name to search (for example we can
        search type "image" with keywords "2005 vacation"), or if object_type
        is None (default), then all types are searched.

        This function returns a list of (object_type, object_id) tuples
        which match the query.  The list is sorted by score (with the
        highest score first).
        """
        t0=time.time()
        # Fetch number of files that are keyword indexed.  (Used in score
        # calculations.)
        row = self._db_query_row("SELECT value FROM meta WHERE attr='keywords_objectcount'")
        objectcount = int(float(row[0]))

        # Convert words string to a tuple of lower case words.
        words = tuple(str_to_unicode(words).lower().split())
        # Remove words that aren't indexed (words less than MIN_WORD_LENGTH
        # characters, or and words in the stop list).
        words = filter(lambda x: len(x) >= MIN_WORD_LENGTH and x not in STOP_WORDS, words)
        words_list = _list_to_printable(words)
        nwords = len(words)

        if nwords == 0:
            return []

        # Find word ids and order by least popular to most popular.
        rows = self._db_query("SELECT id,word,count FROM words WHERE word IN %s ORDER BY count" % words_list)
        save = map(lambda x: x.lower(), words)
        words = {}
        ids = []
        for row in rows:
            if row[2] == 0:
                return []

            # Give words weight according to their order
            order_weight = 1 + len(save) - list(save).index(row[1])
            words[row[0]] = {
                "word": row[1],
                "count": row[2],
                "idf_t": math.log(objectcount / row[2] + 1) + order_weight,
                "ids": {}
            }
            ids.append(row[0])
            # print "WORD: %s (%d), freq=%d/%d, idf_t=%f" % (row[1], row[0], row[2], objectcount, words[row[0]]["idf_t"])

        # Not all the words we requested are in the database, so we return
        # 0 results.
        if len(ids) < nwords:
            return []

        if object_type:
            # Resolve object type name to id
            object_type = self._get_type_id(object_type)

        results, state = {}, {}
        for id in ids:
            results[id] = {}
            state[id] = {
                "offset": [0]*11,
                "more": [True]*11,
                "count": 0,
                "done": False
            }

        all_results = {}
        if limit == None:
            limit = objectcount

        sql_limit = min(limit*3, 200)
        finished = False
        nqueries = 0

        # Keep a dict keyed on object_id that we can use to narrow queries
        # once we have a full list of all objects that match a given word.
        id_constraints = None
        while not finished:
            for rank in range(10, -1, -1):
                for id in ids:
                    if not state[id]["more"][rank] or state[id]["done"]:
                        # If there's no more results at this rank, or we know
                        # we've already seen all the results for this word, we
                        # don't bother with the query.
                        continue

                    q = "SELECT object_type,object_id,frequency FROM " \
                        "words_map WHERE word_id=? AND rank=? %s %%s" \
                        "LIMIT ? OFFSET ?"

                    if object_type == None:
                        q %= ""
                        v = (id, rank, sql_limit, state[id]["offset"][rank])
                    else:
                        q %= "AND object_type=?"
                        v = (id, rank, object_type, sql_limit, state[id]["offset"][rank])

                    if id_constraints:
                        # We know about all objects that match one or more of the other
                        # search words, so we add the constraint that all rows for this
                        # word match the others as well.  Effectively we push the logic
                        # to generate the intersection into the db.
                        # This can't benefit from the index if object_type is not specified.
                        q %= " AND object_id IN %s" % _list_to_printable(tuple(id_constraints))
                    else:
                        q %= ""

                    rows = self._db_query(q, v)
                    nqueries += 1
                    state[id]["more"][rank] = len(rows) == sql_limit
                    state[id]["count"] += len(rows)

                    for row in rows:
                        results[id][row[0], row[1]] = row[2] * words[id]["idf_t"]
                        words[id]["ids"][row[1]] = 1

                    if state[id]["count"] >= words[id]["count"] or \
                       (id_constraints and len(rows) == len(id_constraints)):
                        # If we've now retrieved all objects for this word, or if
                        # all the results we just got now intersect with our
                        # constraints set, we're done this word and don't bother
                        # querying it at other ranks.
                        #print "Done word '%s' at rank %d" % (words[id]["word"], rank)
                        state[id]["done"] = True
                        if id_constraints is not None:
                            id_constraints = id_constraints.intersection(words[id]["ids"])
                        else:
                            id_constraints = Set(words[id]["ids"])


                # end loop over words
                for r in reduce(lambda a, b: Set(a).intersection(Set(b)), results.values()):
                    all_results[r] = 0
                    for id in ids:
                        if r in results[id]:
                            all_results[r] += results[id][r]

                # If we have enough results already, no sense in querying the
                # next rank.
                if limit > 0 and len(all_results) > limit*2:
                    finished = True
                    #print "Breaking at rank:", rank
                    break

            # end loop over ranks
            if finished:
                break

            finished = True
            for index in range(len(ids)):
                id = ids[index]

                if index > 0:
                    last_id = ids[index-1]
                    a = results[last_id]
                    b = results[id]
                    intersect = Set(a).intersection(b)

                    if len(intersect) == 0:
                        # Is there any more at any rank?
                        a_more = b_more = False
                        for rank in range(11):
                            a_more = a_more or state[last_id]["more"][rank]
                            b_more = b_more or state[id]["more"][rank]

                        if not a_more and not b_more:
                            # There's no intersection between these two search
                            # terms and neither have more at any rank, so we
                            # can stop the whole query.
                            finished = True
                            break

                # There's still hope of a match.  Go through this term and
                # see if more exists at any rank, increasing offset and
                # unsetting finished flag so we iterate again.
                for rank in range(10, -1, -1):
                    if state[id]["more"][rank] and not state[id]["done"]:
                        state[id]["offset"][rank] += sql_limit
                        finished = False

            # If we haven't found enough results after this pass, grow our
            # limit so that we expand our search scope.  (XXX: this value may
            # need empirical tweaking.)
            sql_limit *= 10

        # end loop while not finished
        keys = all_results.keys()
        keys.sort(lambda a, b: cmp(all_results[b], all_results[a]))
        if limit > 0:
            keys = keys[:limit]

        #print "* Did %d subqueries" % (nqueries), time.time()-t0, len(keys)
        return keys
        #return [ (all_results[file], file) for file in keys ]

    def get_db_info(self):
        """
        Returns a dict of information on the database:
            count: dict of object types holding their counts
            total: total number of objects in db
            types: dict keyed on object type holding a dict:
                attrs: dict of attributes
                idx: list of multi-column indices
            wordcount: number of words in the keyword index
        """
        info = {
            "count": {},
            "types": {}
        }
        for name in self._object_types:
            id, attrs, idx = self._object_types[name]
            info["types"][name] = {
                "attrs": attrs,
                "idx": idx
            }
            row = self._db_query_row("SELECT COUNT(*) FROM objects_%s" % name)
            info["count"][name] = row[0]


        row = self._db_query_row("SELECT value FROM meta WHERE attr='keywords_objectcount'")
        info["total"] = int(row[0])

        row = self._db_query_row("SELECT COUNT(*) FROM words")
        info["wordcount"] = int(row[0])
        return info


    def vacuum(self):
        # We need to do this eventually, but there's no index on count, so
        # this could potentially be slow.  It doesn't hurt to leave rows
        # with count=0, so this could be done intermittently.
        self._db_query("DELETE FROM words WHERE count=0")
        self._db_query("VACUUM")

