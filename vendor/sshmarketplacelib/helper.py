'''
Created on Nov 16, 2021

@author: cesare
'''
import pandas as pd
import requests
import re
import pickle
import os.path
import os
import json
import numpy as np
import yaml
import multiprocessing
from multiprocessing.pool import Pool
import errno

import pathlib
from collections import defaultdict

class Util(object):

    def __init__(self):
        '''
        Constructor
        '''
        if (os.path.isdir('data/')):
            self.datadir='data/';
        else:
            if (os.path.isdir('../data/')):
                self.datadir='../data';
            else:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "data")


        if (os.path.isfile('config.yaml')):
            self.configfile="config.yaml"
        else:
            if (os.path.isfile('../config.yaml')):
                self.configfile="../config.yaml"
            else:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "config.yaml")

        try:
            with open(self.configfile, 'r') as stream:
                try:
                    self.conf=yaml.safe_load(stream)
                except yaml.YAMLError as exc:
                    print(exc)
        except FileNotFoundError:
            print('Warning config.yaml file not present! Please create it and set the values, store it in the main directory')
        self.MPAPIserver=self.conf['API']['SERVER']
        self.MPserver=self.conf['MARKETPLACE']['SERVER']
        self.server=self.conf['MARKETPLACE']['SERVER']
        self.userId=self.conf['API']['USER']
        self.passW=self.conf['API']['PASSWORD']
        self.allCategories=self.conf['CATEGORIES']
        self.dataset_entrypoints=self.conf['DATASET_ENTRYPOINTS']
        for key, value in self.dataset_entrypoints.items():
            self.dataset_entrypoints[key] = self.MPAPIserver+value
        self.empty_description=self.conf['EMPTY_DESCRIPTION_VAL']
        self.list_of_properties_url=self.conf['DATASET_ENTRYPOINTS']['list_of_properties']
        self.url_all_properties=['accessibleAt','terms-of-use-url', 'access-policy-url', 'privacy-policy-url', 'see-also', 'user-manual-url', 'service-level-url', 'thumbnail']
        self.url_dynamic_properties=['terms-of-use-url', 'access-policy-url', 'privacy-policy-url', 'see-also', 'user-manual-url', 'service-level-url', 'thumbnail']



        self.acceptedops=['=','<','>']


    def _getMPUrl(self, dataset):
        if isinstance(dataset, pd.DataFrame):
            if dataset.empty:
                print ("Error in building MPUrl: a non empty dataframe is required")
                return
        else:
            print ("Error: a dataframe is required!")
        if 'category' in dataset.columns:
            #print (dataset.columns)
            dataset['tempurl'] = dataset['category'].apply(lambda y: y+'/')
        else:
            if 'externalIds' in dataset.columns:
                dataset['tempurl'] = dataset['id'].apply(lambda y: str(y))

        #print (dataset['tempurl'])
        if 'MPUrl' in dataset.columns:
            dataset=dataset.drop(columns="MPUrl", axis=1)

        if 'persistentId' in dataset.columns:
            dataset.insert(0, 'MPUrl', dataset.tempurl+dataset['persistentId'])
        else:
            if 'id' in dataset.columns:
                dataset.insert(0, 'MPUrl', 'actors/'+dataset.tempurl)

        if 'tempurl' in dataset.columns:
            dataset=dataset.drop(columns='tempurl',axis=1)
        return dataset

    def _load_snapshot(self):
        """Load the latest full_items_<ts>.json snapshot from the data directory."""
        data_path = pathlib.Path(self.datadir)
        existing = sorted(data_path.glob("full_items_*.json"), key=lambda p: p.stat().st_mtime)
        if not existing:
            raise FileNotFoundError(f"No full_items_*.json snapshots found in {self.datadir}")
        return pd.read_json(existing[-1], orient="records")

    def getAllItemsBySources(self ):

        """

        Returns the number of items provided by every source.

        """

        df_items = self._load_snapshot()
        df_items['source.label'] = df_items['source.label'].apply(lambda y: 'NA' if pd.isnull(y) else y)
        return df_items['source.label'].value_counts()

    def getItemsBySources(self, itemscategory):

        """

        Returns the number of items provided by every source for a specific category.

        Parameters:
        -----------

        itemcategory : String
            The category

        """

        df_items = self._load_snapshot()
        df = df_items[df_items['category'] == itemscategory]
        if df.empty:
            print('Not loaded or empty dataset: ' + itemscategory)
            return pd.DataFrame()
        return df['source.label'].value_counts()

    def getCategoriesBySources(self):

        """

        Returns the number of items for every category provided by every data source.


        """

        df_items = self._load_snapshot()
        df_items_abs = df_items.groupby(['category', 'source.label']).count()['label'].unstack('category')
        df_items_abs = df_items_abs.T.fillna(0).round()
        df_items_abs.index.names = ['Categories']
        return df_items_abs

    def getContributors(self):

        """

        Returns the contributors of items stored in the local dataset.

        """

        df_items = self._load_snapshot()
        items = pd.json_normalize(
            data=df_items.to_dict(orient='records'),
            record_path='contributors',
            meta=['label', 'persistentId', 'category'],
            errors='ignore'
        )
        if items.empty:
            return pd.DataFrame()
        cols = items.columns.tolist()
        cols = cols[-3:] + cols[:-3]
        return items[cols]

    def getAllProperties(self, dataset):
        """
        Extracts and flattens all 'properties' from a given dataset or collection of datasets.

        Parameters
        ----------
        dataset : DataFrame | dict[str, DataFrame] | list[DataFrame]
            Either:
            - A single DataFrame containing an embedded 'properties' column
            - A dict of DataFrames (e.g. category_dfs)
            - A list/tuple of DataFrames

        Returns
        -------
        DataFrame
            Flattened DataFrame of all properties with 'ts_' meta columns added.
            Columns include e.g. ['ts_label', 'ts_persistentId', 'ts_category', 'type.code', 'value', ...]
        """
        import pandas as pd
        import numpy as np

        dfs = []

        # --- Normalize input ---
        if isinstance(dataset, pd.DataFrame):
            datasets = [dataset]
        elif isinstance(dataset, dict):
            datasets = dataset.values()
        elif isinstance(dataset, (list, tuple)):
            datasets = dataset
        else:
            raise TypeError(
                f"`dataset` must be a DataFrame, dict of DataFrames, or list/tuple of DataFrames; got {type(dataset)}"
            )

        # --- Process each DataFrame ---
        for df in datasets:
            if not isinstance(df, pd.DataFrame):
                print(f"Skipping non-DataFrame entry: {type(df)}")
                continue

            # Skip if missing the 'properties' column
            if "properties" not in df.columns:
                print("Warning: DataFrame has no 'properties' column — skipped.")
                continue

            # Use current category (if available)
            category = df.get("category", pd.Series(["unknown"] * len(df))).iloc[0]

            # Normalize the 'properties' JSON structures
            try:
                flattened = pd.json_normalize(
                    data=df.to_dict(orient="records"),
                    record_path="properties",
                    meta_prefix="ts_",
                    meta=["label", "persistentId", "category"],
                    errors="ignore"
                )
                dfs.append(flattened)
            except Exception as e:
                print(f"Error flattening {category}: {e}")

        if not dfs:
            print("Empty dataset")
            return pd.DataFrame()

        df_items = pd.concat(dfs, ignore_index=True)

        # --- Clean empty lists ---
        if "type.allowedVocabularies" in df_items.columns:
            df_items["type.allowedVocabularies"] = df_items["type.allowedVocabularies"].apply(
                lambda y: np.nan if isinstance(y, (list, tuple)) and len(y) == 0 else y
            )

        return df_items

    def getProperties(self, dataset=''):

        """

        Returns all the properties of items stored in the given dataset.

        Parameters:
        -----------

        dataset : DataFrame
            The dataset where the properties are searched

        """
        returned_values=['id', 'category', 'label', 'persistentId', 'accessibleAt', 'description', 'relatedItems', 'media', 'source.label', 'source.url',
                         'type.code', 'type.label', 'type.type', 'type.groupName', 'type.allowedVocabularies', 'concept.code', 'concept.vocabulary.code',
                         'concept.vocabulary.scheme', 'concept.vocabulary.namespace', 'concept.vocabulary.label', 'concept.vocabulary.closed',
                         'concept.label', 'concept.notation', 'concept.uri', 'concept.candidate', 'value', 'concept.definition']
        df_snapshot = self._load_snapshot()
        df_items = self.getAllProperties(df_snapshot)
        if isinstance(dataset, pd.DataFrame) and not dataset.empty:
            df_merged = pd.merge(left=dataset, right=df_items, left_on='persistentId', right_on='ts_persistentId')
            if 'source.label_x' in df_merged.columns:
                df_merged.rename(columns={'source.label_x': 'source.label', 'ts_label': 'label'}, inplace=True)
            return df_merged[[c for c in returned_values if c in df_merged.columns]]
        df_items.rename(columns={'ts_persistentId': 'persistentId', 'ts_label': 'label', 'ts_category': 'category'}, inplace=True)
        return df_items


    def getAllPropertiesBySources(self, dataset):
        """
        Returns all dynamic properties stored in the provided dataset(s).
        Each row is a dynamic property with key item attributes joined in:
        - persistentId, label, source.label, category, status (if present)

        Parameters
        ----------
        dataset : pandas.DataFrame | dict[str, pandas.DataFrame] | list[pandas.DataFrame]
            One or more item DataFrames. Each should have a 'properties' column
            that contains a list of dicts (dynamic properties).
        """
        import pandas as pd
        import numpy as np

        # ---------- Normalize input ----------
        if isinstance(dataset, pd.DataFrame):
            datasets = [dataset]
        elif isinstance(dataset, dict):
            datasets = list(dataset.values())
        elif isinstance(dataset, (list, tuple)):
            datasets = list(dataset)
        else:
            raise TypeError(
                f"`dataset` must be a DataFrame, dict of DataFrames, or list/tuple of DataFrames; got {type(dataset)}"
            )

        if not datasets:
            return pd.DataFrame()

        prop_frames = []
        item_frames = []

        # ---------- Collect items and their properties ----------
        for i, df in enumerate(datasets):
            if not isinstance(df, pd.DataFrame):
                # skip non-DF entries
                continue
            if df.empty:
                continue

            # Items (top-level columns)
            # We'll only keep columns that are relevant if they exist
            keep_cols = [c for c in ["persistentId", "label", "source.label", "category", "status"] if c in df.columns]
            items = df[keep_cols].copy() if keep_cols else df.copy()
            item_frames.append(items)

            # Properties (nested under 'properties')
            if "properties" not in df.columns:
                # nothing dynamic here, skip
                continue

            # Flatten properties; keep meta columns so we can merge back
            # Prefer persistentId as stable join key; also keep label/category for fallback/debug
            try:
                props_flat = pd.json_normalize(
                    data=df.to_dict(orient="records"),
                    record_path="properties",
                    meta_prefix="ts_",
                    meta=[c for c in ["persistentId", "label", "category"] if c in df.columns],
                    errors="ignore"
                )
            except Exception as e:
                # if a row has malformed 'properties', skip it (but keep others)
                print(f"Warning: error flattening properties in dataset index {i}: {e}")
                continue

            # Optional cleanup similar to your original
            if "type.allowedVocabularies" in props_flat.columns:
                props_flat["type.allowedVocabularies"] = props_flat["type.allowedVocabularies"].apply(
                    lambda v: np.nan if isinstance(v, (list, tuple)) and len(v) == 0 else v
                )

            prop_frames.append(props_flat)

        if not prop_frames or not item_frames:
            return pd.DataFrame()

        df_properties = pd.concat(prop_frames, ignore_index=True)
        df_items = pd.concat(item_frames, ignore_index=True)

        if df_properties.empty or df_items.empty:
            return pd.DataFrame()

        # ---------- Merge properties with items ----------
        # Best: merge on persistentId (ts_persistentId from props_flat)
        # Fallback: merge on label (ts_label) if persistentId not present
        merged = None
        if "ts_persistentId" in df_properties.columns and "persistentId" in df_items.columns:
            merged = pd.merge(
                df_properties, df_items,
                left_on="ts_persistentId", right_on="persistentId",
                how="left", suffixes=("", "")
            )
        elif "ts_label" in df_properties.columns and "label" in df_items.columns:
            merged = pd.merge(
                df_properties, df_items,
                left_on="ts_label", right_on="label",
                how="left", suffixes=("", "")
            )
        else:
            # If neither key exists, return properties as-is (still useful)
            merged = df_properties.copy()

        # ---------- Optional: enrich with MP URLs if your helper expects item rows ----------
        if hasattr(self, "_getMPUrl"):
            try:
                merged = self._getMPUrl(merged)
            except Exception as e:
                print(f"Warning: _getMPUrl failed: {e}")

        return merged




    def getPropertiesValuesFrequency(self, itemscategory, propertyname):
        df_all = self._load_snapshot()
        df_cat = df_all[df_all['category'] == itemscategory]
        if df_cat.empty:
            return pd.DataFrame()
        df_items = pd.json_normalize(
            data=df_cat.to_dict(orient='records'),
            record_path='properties',
            meta_prefix='ts_',
            meta=['label'],
            errors='ignore'
        )
        if 'type.allowedVocabularies' in df_items.columns:
            df_items['type.allowedVocabularies'] = df_items['type.allowedVocabularies'].apply(
                lambda y: np.nan if isinstance(y, (list, tuple)) and len(y) == 0 else y
            )
        if propertyname not in df_items.columns:
            return pd.DataFrame()
        return df_items[propertyname].value_counts()

    #Updated 2025
    def getDuplicates(self, dataset, props=''):
        """
        Returns all rows with duplicate values in specified columns.

        Parameters:
        -----------
        dataset : DataFrame
            The dataset to search (MUST be a DataFrame)
        props : str (optional)
            Comma-separated columns to check for duplicates (e.g., "label,version")
        """
        #CHECK TYPE FIRST (before any column access)
        if not isinstance(dataset, pd.DataFrame):
            print("Error: dataset must be a pandas DataFrame, but is of type", type(dataset))
            return  # EARLY EXIT - prevents all column access errors

        if dataset.empty:
            print("Error: dataset cannot be empty")
            return

        # Rest of the function (safe to run now)
        if not props.strip():
            return dataset[dataset.astype(str).duplicated(keep=False)].reset_index(drop=True)

        properties = [p.strip() for p in props.replace(" ", "").split(',')]
        invalid_cols = [c for c in properties if c not in dataset.columns]
        if invalid_cols:
            print(f"Error: Invalid columns: {', '.join(invalid_cols)}")
            return

        dataset = self._getMPUrl(dataset)
        return dataset[dataset.duplicated(subset=properties, keep=False)].reset_index(drop=True)

    def getDuplicatedActorsWithItems(self, dataset, props=''):

        """

        Returns all the actors of dataset having duplicated values in
        the properties/attributes defined in the props parameter that has at least one associated item.

        Parameters:
        -----------

        dataset : DataFrame
            The dataset where the duplicates are searched
        props: String (optional)
            The property/attribute or list of properties/attributes to be used as filter

        """

        if isinstance(dataset, pd.DataFrame):
            if dataset.empty:
                print ("Error: a not empty dataframe is required")
                return
        else:
            print ("Error: a dataframe is required!")
            return
        if props.strip()!='':
            properties=props.replace(" ", "").split(',')
            for attr in properties:
                if not attr in dataset.columns:
                    print (f"Error: {attr} not a valid attribute")
                    return
        extcontr_df=self.getContributors()
        test_tmp=pd.merge(left=dataset, right=extcontr_df[['persistentId', 'label', 'category','actor.id','role.label']], left_on='id', right_on='actor.id')
        testdup=test_tmp[test_tmp.duplicated(subset=properties, keep=False)]
        testdup=testdup[['MPUrl', 'id', 'name', 'externalIds','affiliations','website','role.label','persistentId','label','category']]
        df_tmp=testdup.groupby('name')['id'].apply(set).reset_index(name='Id')
        df_tmp['isDuplicated']=df_tmp['Id'].apply(lambda y: 'yes' if len(y)>1 else 'no')
        df_tmpduplicated=df_tmp[df_tmp.isDuplicated=='yes']
        tmp_ex=df_tmpduplicated.explode('Id')
        test_te=tmp_ex[['Id', 'isDuplicated']]
        test_res=pd.merge(left=test_te, right=testdup, left_on='Id', right_on='id')
        test_res=test_res.drop(columns='Id',axis=1)
        test_res['item']=test_res.category+'/'+test_res.persistentId
        test_set=test_res.groupby(['MPUrl', 'id', 'name'])['item'].apply(list).reset_index(name='itemPersistentId')
        return test_res, test_set.sort_values('name')



    def getItemsWithNullValues(self, props, all=True):

        """

        Returns the items in the local dataset having null values for a list of properties/attributes.

        Parameters:
        -----------

        props: String
            The property/attribute or list of properties/attributes to be checked. If it is empty or
            not set the numbers of null values for all the properties/attributes are returned.

        all: boolean (optional)
            If 'True' (default) the items where all the properties/attributes are null are returned,
            if ' False' the items where at least one of the properties/attributes are null are returned

        """

        properties=[]
        if props.strip()!='':
            properties=props.replace(" ", "").split(',')
        else:
            print ("A list of properties must be defined")
            return

        df_items = self._load_snapshot()
        temp_ed_str=self.empty_description.replace(".","")
        df_items = df_items.replace(self.empty_description, np.nan)
        df_items = df_items.replace(temp_ed_str, np.nan)
        for col in ['externalIds', 'contributors', 'accessibleAt', 'relatedItems', 'properties', 'media']:
            if col in df_items.columns:
                df_items[col] = df_items[col].apply(lambda y: np.nan if isinstance(y, (list, dict)) and len(y) == 0 else y)

        # Top-level columns are checked directly; dynamic properties are looked up in the nested 'properties' array
        top_level_props = [pr for pr in properties if pr in df_items.columns]
        dynamic_props = [pr for pr in properties if pr not in df_items.columns]
        for pr in dynamic_props:
            df_items[pr] = df_items['properties'].apply(
                lambda raw: np.nan if not isinstance(raw, list) or not any(
                    (p.get('type') or {}).get('code') == pr for p in raw if isinstance(p, dict)
                ) else True
            )

        all_props = top_level_props + dynamic_props
        if not all_props:
            print("No valid properties found")
            return pd.DataFrame()

        df_items_mask=df_items[all_props].apply(lambda x: x.isnull())
        if all:
            df_items=df_items[df_items_mask.all(axis=1)]
        else:
            df_items=df_items[df_items_mask.any(axis=1)]
        df_items['tempurl'] = df_items['category'].apply(lambda y: y+'/' if len(y)>0 else y)
        df_items['MPUrl']=df_items['tempurl']+df_items['persistentId']
        df_items=df_items.drop(columns='tempurl',axis=1)
        return df_items

    #rendering functions

    def make_clickable(self, val):
    # target _blank to open new window
        return '<a target="_blank" href="'+self.MPserver+'{}">{}</a>'.format(val, val)


    def lists_to_list(self, nested_lists):
        outer_list = []
        #print (f'input {nested_lists}')
        for el in nested_lists:

            if type(el) == list:
                self.lists_to_list(el)
            else:

                if type(el) == dict:

                    jsel=json.dumps(el, sort_keys=True)
                    outer_list.append(jsel)
                else:

                    outer_list.append(el)
        #print (f' output {set(outer_list)}')
        return set(outer_list)


    #get related items

    def getRelatedItems(self, itemcategories, *nrelitems):
        returned_fields=['MPUrl','persistentId', 'category', 'label', 'relation.label',  'relitem_persistentId', 'relItem_category', 'relItem_label', 'relItem_description', 'relation.code', 'value']
        wid=['workflowId']
        no_rel_items_fields=['MPUrl','persistentId', 'category', 'label', 'relatedItems', 'value']
        dfs=[]

        no_related_items=pd.DataFrame()
        if itemcategories.strip()=='all':
                categories=self.allCategories
        else:
            if itemcategories.strip()!='':
                categories=itemcategories.replace(" ", "").split(',')
                for ca in categories:
                    if ca.strip() not in self.allCategories:
                        print ('Wrong Category: '+ca)
            else:
                print ('No category defined!')
                return
        df_all = self._load_snapshot()
        for cate in categories:
            items = df_all[df_all['category'] == cate].copy()
            if items.empty:
                continue
            if not nrelitems:
                selected_items=items[items['relatedItems'].map(len)>0]
            else:
                if type(nrelitems[0])==int and len (nrelitems)==1:
                    nval=nrelitems[0]
                    l = list(nrelitems)
                    l.insert(1, nval)
                    l[0]='>'
                    nrelitems=tuple(l)

                if type(nrelitems[0])!=int and len (nrelitems)==1:
                    l = list(nrelitems)
                    l.insert(1, 0)
                    l[0]='>'
                    nrelitems=tuple(l)

                if nrelitems[0] not in self.acceptedops or len (nrelitems)>2:
                    print('wrong parameters ')
                    return
                if (nrelitems[0].strip()=='=' and nrelitems[1]<1) or (nrelitems[0].strip()=='<' and nrelitems[1]==1):
                    #print (items['relatedItems'])
                    items['value']=items['relatedItems'].map(len)
                    no_related_items=items[items['relatedItems'].map(len)==0]


                    #return selected_items[['persistentId', 'category', 'label', 'relatedItems']]
                if nrelitems[0].strip()=='=' and nrelitems[1]>0:
                    #print (nrelitems[1])
                    selected_items=items[items['relatedItems'].map(len)==nrelitems[1]]
                if nrelitems[0].strip()=='>':
                    selected_items=items[items['relatedItems'].map(len)>nrelitems[1]]
                if nrelitems[0].strip()=='<' and nrelitems[1]>1:
                    selected_items=items[items['relatedItems'].map(len)<nrelitems[1]]

            items= pd.json_normalize(data=items.to_dict(orient='records'), record_path='relatedItems', meta_prefix='item_', meta=['label', 'persistentId', 'category'], errors='ignore')
            #print (category)
            if no_related_items.empty:
                selected_items['value']=selected_items['relatedItems'].map(len)

                searched_items=pd.merge(left=selected_items, right=items, left_on='persistentId', right_on='item_persistentId')

                if not searched_items.empty:
                    dfs.append(searched_items)


            else:
                dfs.append (no_related_items)
                returned_fields=no_rel_items_fields
        if not dfs:
            print ('getRelatedItems(itemcategories, *nrelitems): no values found')
            return pd.DataFrame(columns=returned_fields)
        df_items= pd.concat(dfs, ignore_index=True)
        #return df_items[['item_persistentId', 'item_category', 'item_label', 'relation.label',  'persistentId', 'category', 'label', 'workflowId', 'description', 'relation.code']]
        df_items.rename(columns = {'item_persistentId': 'persistentId', 'item_category':'category', 'item_label':'label','persistentId_y': 'relitem_persistentId', 'category_y': 'relItem_category', 'label_y': 'relItem_label', 'description_y': 'relItem_description'}, inplace = True)

        if df_items.empty:
            print ('getRelatedItems(itemcategories, *nrelitems): no values found')
            return pd.DataFrame(columns=returned_fields)


        df_items=self._getMPUrl(df_items)
        #df_items['MPUrl'] = df_items['MPUrl'].apply(lambda y: y if len(y)>0 else y)


        if wid[0] in df_items.columns:
            returned_fields=returned_fields+wid

        df_items=df_items[returned_fields].sort_values('label')

        df_items.reset_index(inplace=True)
        return df_items[returned_fields]

    def getAllRelatedItems(self):
        df_snapshot = self._load_snapshot()
        rel_items = pd.json_normalize(
            data=df_snapshot.to_dict(orient='records'),
            record_path='relatedItems',
            meta_prefix='item_',
            meta=['label', 'persistentId', 'category'],
            errors='ignore'
        )
        cols = ['item_persistentId', 'item_category', 'item_label', 'relation.label', 'persistentId', 'category', 'label', 'workflowId', 'description', 'relation.code']
        return rel_items[[c for c in cols if c in rel_items.columns]]

#addition 2025
def parse_properties(raw):
    """raw can be a list[dict] or a JSON string of that list."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []

def extract_code_value(prop):
    """Return (code, value) from a single property dict."""
    if not isinstance(prop, dict):
        return None, None

    code = (prop.get("type") or {}).get("code")

    # Prefer explicit 'value' first (booleans, free text, etc.)
    val = prop.get("value")
    if val is not None and val != "":
        # normalize boolean strings
        if isinstance(val, str) and val.upper() in {"TRUE", "FALSE"}:
            val = (val.upper() == "TRUE")
        return code, val

    # Otherwise pull from concept object
    c = prop.get("concept") or {}
    if isinstance(c, dict):
        # choose a sensible fallback order
        val = c.get("code") or c.get("label") or c.get("notation") or c.get("uri")
        return code, val

    return code, None

def properties_to_dict(raw_props, dedupe=True, as_counts=False):
    """
    Convert 'properties' to:
    - dict[code] -> list of values   (default)
    - or counts per code if as_counts=True
    """
    props = parse_properties(raw_props)

    if as_counts:
        counts = defaultdict(int)
        for p in props:
            code, val = extract_code_value(p)
            if code: counts[code] += 1
        return dict(counts)

    out = defaultdict(list)
    for p in props:
        code, val = extract_code_value(p)
        if code is None:
            continue
        if val is not None:
            out[code].append(val)
        else:
            # still record the code with a None placeholder if you want
            out[code]  # touch key
    if dedupe:
        out = {k: sorted(set(v)) for k, v in out.items()}
    return dict(out)
import json

# --- helpers ---
def _parse_props(raw):
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []

def _has_value(v):
    # non-empty string/list/dict/True/number counts as present
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, tuple, dict, set)):
        return len(v) > 0
    if isinstance(v, bool):
        return v
    return True  # numbers etc.

def _normalize_item_type(item_type: str) -> str:
    # map common aliases to your canonical keys
    aliases = {
        "tool-or-service": "tools-services",
        "tools-services": "tools-services",
        "training-material": "training-materials",
        "training-materials": "training-materials",
        "publication": "publications",
        "publications": "publications",
        "dataset": "datasets",
        "datasets": "datasets",
        "workflow": "workflows",
        "workflows": "workflows",
    }
    return aliases.get(item_type, item_type)

# --- main validator ---
def validate_metadata(json_data, item_type):
    """
    Validate a single item dict against the metadata profile for its type.
    Returns: (label, results_dict, suggestions_list, extras_dict, overall_score_int)
    """
    # Profiles
    metadata_fields = {
        "tools-services": {
            "Generic Metadata": ["label", "description", "contributors", "accessibleAt", "externalIds", "media", "thumbnail", "relatedItems"],
            "Categorisation Metadata": ["activity", "keyword", "discipline", "language", "intended-audience", "resource-category"],
            "Context Metadata": ["see-also"],
            "Access Metadata": ["license"],
            "Technical Metadata": ["technology-readiness-level", "version"],
        },
        "training-materials": {
            "Generic Metadata": ["label", "description", "contributors", "accessibleAt", "externalIds", "media", "thumbnail", "relatedItems"],
            "Categorisation Metadata": ["activity", "keyword", "discipline", "language", "intended-audience", "resource-category"],
            "Context Metadata": ["see-also"],
            "Access Metadata": ["license"],
            "Technical Metadata": [],
        },
        "publications": {
            "Generic Metadata": ["label", "description", "contributors", "accessibleAt", "externalIds", "media", "thumbnail", "relatedItems"],
            "Categorisation Metadata": ["activity", "keyword", "discipline", "language", "resource-category"],
            "Context Metadata": ["see-also"],
            "Access Metadata": ["license"],
            "Bibliographic metadata": ["publication-type", "publisher", "publication-place", "year", "journal", "conference", "volume", "issue", "pages"],
        },
        "datasets": {
            "Generic Metadata": ["label", "description", "contributors", "accessibleAt", "externalIds", "media", "thumbnail", "relatedItems"],
            "Categorisation Metadata": ["activity", "keyword", "discipline", "language", "resource-category"],
            "Context Metadata": ["see-also"],
            "Access Metadata": ["license"],
            "Bibliographic metadata": ["publisher", "year"],
        },
        "workflows": {
            "Generic Metadata": ["label", "description", "contributors", "externalIds", "media", "thumbnail", "relatedItems"],
            "Categorisation Metadata": ["activity", "keyword", "discipline", "language", "standard", "resource-category"],
            "Context Metadata": ["see-also"],
            "Access Metadata": ["license"],
            "Technical Metadata": [],
        },
    }

    results = {
        "Generic Metadata": {},
        "Categorisation Metadata": {},
        "Context Metadata": {},
        "Access Metadata": {},
        "Bibliographic metadata": {},
        "Technical Metadata": {},
    }
    suggestions = []

    itype = _normalize_item_type(item_type)
    if itype not in metadata_fields:
        # Unknown profile: consider everything missing
        return json_data.get("label"), results, ["Unknown item_type profile."], {}, 0

    # Parse properties for category-based checks
    properties = _parse_props(json_data.get("properties", []))

    def check_property(code: str) -> bool:
        for p in properties:
            if not isinstance(p, dict):
                continue
            t = p.get("type") or {}
            if t.get("code") == code:
                # consider present if either value exists OR a concept exists
                if ("value" in p and _has_value(p.get("value"))) or _has_value(p.get("concept")):
                    return True
        return False

    # Validate
    for category, fields in metadata_fields[itype].items():
        if category in {"Categorisation Metadata", "Context Metadata", "Access Metadata", "Technical Metadata", "Bibliographic metadata"}:
            for field in fields:
                ok = check_property(field)
                results[category][field] = ok
                if not ok:
                    suggestions.append(f"Add or update '{field}' in {category}.")
        else:
            # Generic fields live at top-level
            for field in fields:
                ok = _has_value(json_data.get(field))
                results[category][field] = ok
                if not ok:
                    suggestions.append(f"Add or update '{field}' in {category}.")

    # Score
    total_fields = sum(len(v) for v in metadata_fields[itype].values())
    filled_fields = sum(sum(1 for v in cat.values() if v) for cat in results.values())
    overall_score = int((filled_fields / total_fields) * 100) if total_fields else 0

    return json_data.get("label"), results, suggestions, {}, overall_score

def find_items_missing_profile(df, id_col="persistentId", label_col="label"):
    """
    Validate each row against the metadata profile for `item_type` and
    return rows that miss at least one required field/property.
    """
    import pandas as pd

    if df.empty:
        return pd.DataFrame(columns=[id_col, label_col, "missing_fields", "score"])

    rows = []
    for _, row in df.iterrows():
        # Convert the row to a plain dict for the validator
        jd = row.to_dict()
        #we can just get the item type from the category field
        item_type = _normalize_item_type(jd.get("category", ""))
        label, results, suggestions, _, score = validate_metadata(jd, item_type)

        # collect missing fields for this item
        missing = []
        for cat, fields in results.items():
            for f, ok in fields.items():
                if not ok:
                    missing.append(f"{cat}::{f}")

        if missing:
            rows.append({
                id_col: row.get(id_col),
                label_col: label,
                "missing_fields": missing,
                "score": score,
            })
        # add category and source info if available
        if "category" in row:
            rows[-1]["category"] = row["category"]
        if "source.label" in row:
            rows[-1]["source.label"] = row["source.label"]
    out = pd.DataFrame(rows)
    # Optional: sort by worst score first, then number of missing
    if not out.empty:
        out["missing_count"] = out["missing_fields"].apply(len)
        out = out.sort_values(["score", "missing_count"], ascending=[True, False]).reset_index(drop=True)
    return out
