'''
Created on Nov 12, 2021

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
import urllib.request
import errno
from datetime import datetime
from warnings import warn


#from bokeh.util.sampledata import DataFrame
from http.cookies import _getdate
from PIL.ImageFilter import DETAIL

class MPData:
    def __init__(self):
        """
        Method for constructor taking care of loading configurations
        """
        
        self._loadConfig(self)
        
        

    @staticmethod
    def _loadConfig(self) -> str:
        """
        Method to load configurations
        """
        
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
        self.categoryFilter=self.conf['CATEGORY_FILTER_VALUES']
        if 'DEBUG' in self.conf:
            self.debug=self.conf['DEBUG']
        else:
            self.debug=True
    
   
    
    def getMPItems (self, itemscategory, localrepository=False, pages=0):# -> DataFrame :
        
        """
        
        
        Loads data from MP dataset. This method creates a dataframe, stores it in a local repository and returns it to the caller.
        
        Parameters:
        -----------
        itemscategory : str
            The category of items, possible values are: "toolsandservices", "publications", "trainingmaterials", "workflows", "dataset"
        localrepository : boolean, optional
        pages : int, optional
            The number of pages, default is all
        
        Returns:
        --------
        DataFrame: Returning value

        If the localrepository parameter is 'FALSE' or is not specified, the items are downloaded from the MP dataset, 
        if the localrepository parameter is 'TRUE' the items are first searched in the local repository and 
        if they are not present they are downloaded from the remote MP dataset. 
        When the items are downloaded from the remote MP dataset they are stored in locally.
        If the pages parameter is not provided all descriptions are returned, otherwise 20*pages items are returned.
        
        """
        
        url=self.dataset_entrypoints[itemscategory]
        start=1
        df_desc = pd.DataFrame()
        if (localrepository and os.path.isfile(self.datadir+itemscategory+'.pickle')):#local file is explicitly requested and one is available
            print ('getting data from local repository...')
            df2 = pd.read_pickle(self.datadir+itemscategory+'.pickle')
            category=df2.columns[-1]
            items= pd.json_normalize(df2[category])

            rowsnumber=20*pages;
            if rowsnumber==0 or rowsnumber<0 or rowsnumber>items.shape[0]:
                return items
            else:
                returneditems=items.iloc[0:rowsnumber]
                return returneditems
    
        #print (url)
        df_desc_par=pd.read_json(url+'?perpage=20', orient='columns')
        
        df_desc = pd.concat([df_desc, df_desc_par], ignore_index=True)
        if not df_desc.empty:
            if pages==0:
                pages=df_desc.loc[0].pages
            start+=1
            mdx = pd.Series(range(start, pages+1))
            for var in mdx:
                turl = url+"?page="+str(var)+"&perpage=20"
                #print (f'{var} - {turl}')
                try:
                    df_desc_par=pd.read_json(turl, orient='columns')
                except:
                    print(f'SEVERE: Error getting {itemscategory}, items may be not completely loaded. (Error loading {turl})')
                    
                df_desc = pd.concat([df_desc, df_desc_par], ignore_index=True)
            category=df_desc.columns[-1]
            items= pd.json_normalize(df_desc[category])

            if os.path.isfile(self.datadir+itemscategory+'.pickle'):
                os.remove(self.datadir+itemscategory+'.pickle')
            df_desc.to_pickle(self.datadir+itemscategory+'.pickle')
        else:
            if os.path.isfile(self.datadir+itemscategory+'.pickle'):
                os.remove(self.datadir+itemscategory+'.pickle')
            items=pd.DataFrame()
        return (items)
    
    def getAllProperties(self):# -> DataFrame:
        
        """
        Returns all properties
        This method searches the local dataset and returns a DataFrame with all properties.
        
        """
        
        dfs=[]
        for key in self.dataset_entrypoints:
            if os.path.isfile(self.datadir+key+'.pickle') and not key=='actors':
                temp= pd.read_pickle(self.datadir+key+'.pickle')
                category=temp.columns[-1]
                
                items= pd.json_normalize(data=temp[category], record_path='properties', meta_prefix='ts_', meta=['label', 'persistentId', 'category'])
                dfs.append(items)
        df_items= pd.concat(dfs, ignore_index=True)
        if df_items.empty:
            print('Empty dataset')
            return pd.DataFrame()
        df_items['type.allowedVocabularies'] = df_items['type.allowedVocabularies'].apply(lambda y: np.nan if len(y)==0 else y)
        return df_items
    
    
    def getMPKeywordProperties(self, mykw):# -> DataFrame:
        
        """
        Returns keyword properties
        
        """
        
        myurl=self.dataset_entrypoints["keyword"]+mykw
        with urllib.request.urlopen(myurl) as url:
            mdata = json.load(url)
 
        df_kw= pd.DataFrame(mdata["concepts"])
        return df_kw
    
    
    
    def getMPConcepts(self):# -> DataFrame:
        
        """
        Returns MP Concepts
        
        """
        df_concepts = pd.DataFrame()
        items= pd.DataFrame()
        myurl=self.dataset_entrypoints["concepts"]
        # with urllib.request.urlopen(myurl) as url:
        #     mdata = json.load(url)
        #
        # df_kw= pd.DataFrame(mdata["concepts"])
        
        
        with urllib.request.urlopen(myurl+'?perpage=100') as url:
            df_desc_par = json.load(url)
        
        
        df_concepts= pd.DataFrame(df_desc_par["concepts"])
        #df_desc_par=pd.read_json(myurl+'?perpage=100', orient='columns')
        start=1
        
        if not df_concepts.empty:
            pages=df_desc_par['pages']
            start+=1
            mdx = pd.Series(range(start, pages+1))
            for var in mdx:
                turl = myurl+"?page="+str(var)
                #print (f'{var} - {turl}')
                try:
                    with urllib.request.urlopen(turl+'&perpage=100') as murl:
                        df_desc_par = json.load(murl)
                        df_concepts = pd.concat([df_concepts, pd.DataFrame(df_desc_par["concepts"])], ignore_index=True)
                except:
                    print(f'SEVERE: Error getting concepts, items may be not completely loaded. (Error loading {turl})')
        return df_concepts
    
    """
        Methods to update the MP dataset
    
    """
    
    
 
    
    def getBearer(self, entryPoint=''):
        
        """
        This method returns the bearer code needed to execute write operations in the MP dataset
        
        """
    
        headers = {'Content-type': 'application/json'}

        url=self.dataset_entrypoints['login']
        response = requests.post(url, headers=headers, json={"username": self.userId, "password": self.passW})
        
        bearer=response.headers['Authorization']
        return bearer
    
    def createURLCurationProperty(self, props, label, value):
        detail_value=props
        init_val="{'url': ["+" {'"+label+"': " + "'"+ value.strip() +"'}"+ "] }"
        #init_val="test"
        
        if(props.strip()==''):
            return "{'url': ["+' {"'+label+'": '+'"'+value.strip()+'"}'+ ']}'
        if ("{'url': [" not in props):
            temp_val=detail_value.replace('{', "{'url': ["+" {'"+label+"': " + "'"+ value.strip() +"'}"+ "], ")
            return temp_val
        if ("{'url': [" in props):
            #print ('"'+label+'": ')
            if (label.strip()=='accessibleAt' and '"'+label+'": ' in props):
                
                return props
            if ('"'+label+'": '+'"'+value.strip()+'"' in props) or ("'"+label+"': "+"'"+value.strip()+"'" in props):
                
                return props
            
            
            else:
                temp_val=detail_value.replace("{'url': [",  "{'url': ["+" {'"+label+"': " + "'"+ value.strip() +"'}, ")
                return temp_val
    
    
    def updateURLCurationPropertyJson(self, props, label, value):
        
        if (props.strip()!=''):
            detail_value=json.loads(props.replace("'", '"'))
        else:
            pdata={}
            pvalue={}
            pdata["length"] = value
            pvalue[label]=pdata
            
            return (json.dumps(pvalue))
        
        if not label in detail_value:
            
            data = {}
            data["length"] = value
            json_data = json.dumps(data)
            detail_value[label]=json_data
        print (json.dumps(detail_value))
        return json.dumps(detail_value)
    
    
    
    def setURLStatusFlags(self, dataset, itemscategory, curationFlag, curationDetail):
        
        """
        Sets the URLStatus flag for items in dataset and updates the MP data.
        
        Parameters:
        -----------
        
        dataset : DataFrame
            The set of items to be flagged
        itemscategory: String
            The Category of items
        curationFlag: String
            The Curation Flag property
        curationdetail: String
            The Curation Detail property
        
        DEPRECATED: this function should not be used, please use: setHTTPStatusFlags(self, dataset, curationFlag, curationDetail) 
        
        """
        
        
        
        res=pd.DataFrame()
        if os.path.isfile('data/'+itemscategory+'.pickle'):#local file is explicitly requested and one is available
            df_categ_all = pd.read_pickle('data/'+itemscategory+'.pickle')
        else:
            print("ERROR: data not downloaded or wrong category name...")
            return res
        bearer=self.getBearer()
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        #put_url = "https://sshoc-marketplace-api.acdh-dev.oeaw.ac.at/api/tools-services/"
        put_url=self.dataset_entrypoints[itemscategory]+'/'
        #print (put_url)
       
        for index, row in df_categ_all.iterrows():
            
            updateItem=False
            category=df_categ_all.columns[-1]
            
            toolpid=row[category]['persistentId']
            version=row[category]['id']
            resrow={'persistentId': toolpid, 'oldVersion':version}
            statustool=dataset[dataset['persistentId']==toolpid]
            stdf=statustool[['status', 'property']]
            st=statustool['status']
            status_property_value={}
            vvalue=''
            vproperty=''
            value=''
            
            for vindex, vrow in stdf.iterrows():
                
                if (vrow['status']!=403 and vrow['status']!=408 and vrow['status']!=406 and vrow['status']!=420) or (vrow['status']==200):
                    vvalue=str(vrow['status'])
                    vproperty=str(vrow['property'])
                    break       
            myrow=row[category]
            if vvalue.strip()!='' and vvalue.strip()!='200':
                updateItem=True
                print (f'{vvalue}, {vproperty}, {toolpid}, {updateItem}')
                curation_property_exists=False
                curation_detail_exists=False
                curation_property_value={ "type": curationFlag, "value": "TRUE"}

                detail_value='{"url": ['+' {"'+vproperty+'": '+'"'+vvalue+'"}'+ ']}'
                curation_detail_value={ "type": curationDetail, "value": detail_value}

                
                #print (myrow)
                for ind in myrow['properties']:
                    #print (ind)
                    if (ind['type']['code']=='curation-flag-url'):

                        curation_property_exists=True

                    if (ind['type']['code']=='curation-detail'):
                        cur_de_val=ind['value']
                        
                        #print("      found ")
                        #print(ind['value'])
                        #print ("      updating "+toolpid)
                        #print(createURLCurationProperty(cur_de_val, vproperty, vvalue)+'\n')
                        curation_detail_exists=True
                        ind['value']=self.createURLCurationProperty(cur_de_val, vproperty, vvalue)

                        print (f"property already exists, value:  {ind['value']}")
                #add HTTP-status-code property 
                if not curation_property_exists:
                    #print ('appending curation_property_value')
                    myrow['properties'].append(curation_property_value)
                if not curation_detail_exists:
                    print ('appending curation_detail_value ' + self.createURLCurationProperty('', vproperty, vvalue)+"\n\n")
                    curation_detail_value={ "type": curationDetail, "value": self.createURLCurationProperty('', vproperty, vvalue)}
                    #print (f" ------ property before { myrow['properties']} \n")
                    myrow['properties'].append(curation_detail_value)
                    #print (f"{updateItem}, {curation_detail_value}, pid: {toolpid}, \n { myrow['properties']}")
                if curation_property_exists and curation_detail_exists:
                    updateItem=False
    #         else:
    #             #remove curation property if present
    #             #updateItem=False
    #             for nind in myrow['properties']:
    #                 if (nind['type']['code']=='curation-detail'):
    #                     print (f'{vvalue}, {vproperty}, {toolpid}')
    #                     print('remove...?')
    #                     print (nind['value'])
    #                     print ('update '+updateURLCurationPropertyJson (nind['value'], vproperty, vvalue))
                
                    
            
                  
            obj = json.dumps(myrow)
            
            #save the updated description
            if (not self.debug) and updateItem:
                print (f"updating item... ")
                put_result=requests.put(put_url+toolpid, data=obj, headers=put_headers)
                print (put_result)
        if (not self.debug):
            print ('Reloading data from MP server, please wait...')
            self.getMPItems (itemscategory, False)
            print ('done!')
                
        
        return res
    
    
    
    
    def createCurationProperty(self, props, label, value):
        
        if (label.strip()=='description'):
            if (value=='nan'):
                value='0'
            if ('.' in value):
                split_string = value. split(".", 1)
                value=split_string[0]
            if(props.strip()==''): 
                result="{'description': "+' {"length": '+'"'+value.strip()+'"}'+ '}'
                return result
            if(props.strip()!=''):
                if label in props:
                    return props
                new=", 'description': "+' {"length": '+'"'+value.strip()+'"}'+ '}'
                result=new.join(props.rsplit('}',1))
                return result
        if (label.strip()=='coverage'):
            if(props.strip()==''): 
                result="{'coverage': {'nulls': "+"'"+value.strip()+"'}}"
                
                return result
            if(props.strip()!=''):
                if label in props:
                    return props
                new=", 'coverage': "+' {"nulls": '+'"'+value.strip()+'"}'+ '}'
                result=new.join(props.rsplit('}',1))
                return result
    
    
    def setPropertyStatusFlags(self, dataset, itemscategory, curationFlag, curationDetail):
        
        """
        DEPRECATED
        Sets the status flag for items in dataset and updates the MP data.
        
        Parameters:
        -----------
        
        dataset : DataFrame
            The set of items to be flagged
        itemscategory: String
            The Category of items
        curationFlag: String
            The Curation Flag property
        curationdetail: String
            The Curation Detail property
        
        
        """
        
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "data")
    
        res=pd.DataFrame()
        if os.path.isfile('data/'+itemscategory+'.pickle'):#local file is explicitly requested
            df_categ_all = pd.read_pickle('data/'+itemscategory+'.pickle')
        else:
            print("ERROR: data not downloaded or wrong category name...")
            return res
        bearer=self.getBearer()
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        
        put_url=self.dataset_entrypoints[itemscategory]+'/'
        #print (put_url)
        for index, row in df_categ_all.iterrows():
            #print (row)
            category=df_categ_all.columns[-1]

            toolpid=row[category]['persistentId']
            statustool=dataset[dataset['persistentId']==toolpid]
            stdf=statustool[['value', 'property']]
            #st=statustool['value']
           
            vvalue=''
            value=''
            
            for vindex, vrow in stdf.iterrows():
                vvalue=str(vrow['value'])
                vproperty=str(vrow['property'])
                print (f'{vvalue}, {vproperty}, {toolpid}')
                #print (curationFlag['code'])
                break
    
            if vvalue.strip()!='':
                curation_property_exists=False
                curation_detail_exists=False
                curation_property_value={ "type": curationFlag, "value": "TRUE"}

                myrow=row[category]

                for ind in myrow['properties']:
                    #print (ind)
                    updateItem=True
                    
                    if (ind['type']['code']==curationFlag['code']):
                        
                        curation_property_exists=True

                    if (ind['type']['code']=='curation-detail'):
                        cur_de_val=ind['value']
                        print ("      updating "+toolpid)

                        ind['value']=self.createCurationProperty(cur_de_val, vproperty, vvalue)
                        print (ind['value'])
                        curation_detail_exists=True
                
                if not curation_property_exists:
                    print ('append curation_property_value')
                    myrow['properties'].append(curation_property_value)

                if not curation_detail_exists:
                    print ('append curation_detail_value')
                    curation_detail_value={ "type": curationDetail, "value": self.createCurationProperty('', vproperty, vvalue)}
                    myrow['properties'].append(curation_detail_value)
                    print (curation_detail_value)
                
                if curation_property_exists and curation_detail_exists:
                    updateItem=False   
               
                obj = json.dumps(myrow)
                #print (obj)
                #save the updated description
                if (not self.debug) and updateItem:
                    print (f'updating {toolpid}..')
                    put_result=requests.put(put_url+toolpid, data =obj, headers=put_headers)
                
                    print (put_result)
                
        if not self.debug:
            print ('Reloading data from MP server, please wait...')
            self.getMPItems (itemscategory, False)
            print ('done!')        
            return res
    
    
    def updatePropertyValues(self, dataset, itemscategory, prop):
        res=pd.DataFrame()
        if os.path.isfile('data/'+itemscategory+'.pickle'):#local file is explicitly requested
            df_categ_all = pd.read_pickle('data/'+itemscategory+'.pickle')
        else:
            print("ERROR: data not downloaded or wrong category name...")
            return res
        bearer=self.getBearer()
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        
        put_url=self.dataset_entrypoints[itemscategory]+'/'
        category=df_categ_all.columns[-1]
        for index, row in df_categ_all.iterrows():
                  
            label=row[category]['label']
            toolpid=row[category]['persistentId']
            
            statustool=dataset[dataset['label']==label]
            
            stdf=statustool[['label', prop, 'fix']]
            
            myrow=row[category]
            for ind in myrow['properties']:
                    #print (ind)
                if (ind['type']['code']=='curation-flag-url'):
                    print (ind['type']['code']+" "+toolpid)
                    
            upd=False
            for vindex, vrow in stdf.iterrows():
                fix=vrow['fix']
                wrongurl=vrow[prop]
                if (str(fix)!='nan') and (fix.strip()!=wrongurl.strip()):
                    
                    print (f'{fix}, for: {wrongurl}, {label}')
                    candprop=myrow[prop]
                    #if (candprop.isin(wrongurl)):
                    if any(wrongurl in s for s in candprop):
                        upd=True
                        print ('fixing, replacing '+ str(candprop)+' with '+fix+ ' in '+toolpid)
                        myrow[prop]=[fix if x==wrongurl else x for x in myrow[prop]]
                        #print('\n\n')
                        #print(myrow)
                    else:
                        print ('NOT fixing: '+str(candprop))
                        upd=False
            obj = json.dumps(myrow)
            if upd:
                print (f'updating {toolpid}...')
                #save the updated description
                #put_result=requests.put(put_url+toolpid, data =obj, headers=put_headers)
                
                #print (put_result)
    
    def setHTTPStatusFlags(self, dataset, curationFlag, curationDetail):
        
        """
        Sets the URL Status flag for items in dataset and updates the MP data.
        
        Parameters:
        -----------
        
        dataset: DataFrame
            The set of items to be flagged
        curationFlag: String
            The Curation Flag property
        curationDetail: String
            The Curation Detail property
        
        When all items have been flagged the function synchronizes the local dataset with the MP data, it may take several minutes to complete.
               
        """
        
        res=pd.DataFrame()
        dfs=[]
        bearer=self.getBearer()
        
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        
        properties=dataset.property.unique().tolist();
        for key in self.dataset_entrypoints:
            if os.path.isfile('data/'+key+'.pickle') and not key=='actors':#local file is explicitly requested and one is available
                df_categ_all = pd.read_pickle('data/'+key+'.pickle')
            if df_categ_all.empty:
                print('INFO:'+key+' data not downloaded...')
                break;
       
            category=''
            for index, row in df_categ_all.iterrows():
                
                updateItem=False
                category=df_categ_all.columns[-1]
    
                
               
            
                toolpid=row[category]['persistentId']
                version=row[category]['id']
                resrow={'persistentId': toolpid, 'oldVersion':version}
                
               

                statusitems=dataset[dataset['persistentId']==toolpid]
                
                stdf=statusitems[['status', 'property']]
                
                
                # if not toolpid in dataset.persistentId:
                #     mrow, changed= self._removeFlag(row[category], 'curation-flag-url', properties)
                #     print(f"CHANGED {changed}")
                
                status_property_value={}
                vvalue=''
                vproperty=''
                value='' 
                wrongvals={}             
                for vindex, vrow in stdf.iterrows():
                    
                    if (vrow['status']!=403 and vrow['status']!=408 and vrow['status']!=406 and vrow['status']!=420) or (vrow['status']==200):
                        vvalue=str(vrow['status'])
                        vproperty=str(vrow['property'])
                        wrongvals[str(vrow['property'])]=str(vrow['status'])
                        #print (f'{vrow["property"]}, {category} \n')
                        #break       
                myrow=row[category]
                
                for key in wrongvals:
                    vproperty=key
                    vvalue=wrongvals[key]
                    if (not (vvalue.strip() =='200')):
                    #if (vvalue.strip()=='404' ):
                        print (f'The item with PID: {category}/{toolpid} has a *{vvalue}* HTTP status for the property {vproperty}, ({updateItem})')
                        curation_property_exists=False
                        curation_detail_exists=False
                        curation_property_value={ "type": curationFlag, "value": "TRUE"}
        
                        detail_value='{"url": ['+' {"'+vproperty+'": '+'"'+vvalue+'"}'+ ']}'
                        curation_detail_value={ "type": curationDetail, "value": detail_value}
        
                        
                        #print (myrow)
                        for ind in myrow['properties']:
                            
                            if (ind['type']['code']=='curation-flag-url'):
        
                                curation_property_exists=True
        
                            if (ind['type']['code']=='curation-detail'):
                                cur_de_val=ind['value']
                                curation_detail_exists=True
                                #if ind['value'].strip()==vproperty:
                                if vproperty in ind['value'].strip():
                                    print (f"flag property exists, value:  {ind['value']} \n")
                                    updateItem=updateItem or False
                                else:
                                    ind['value']=self.createURLCurationProperty(cur_de_val, vproperty, vvalue)
                                    print (f"Appending curation_detail_flag  {self.createURLCurationProperty(cur_de_val, vproperty, vvalue)}")
                                    updateItem=updateItem or True
                        #add HTTP-status-code property 
                        if not curation_property_exists:
                            #print ('appending curation_property_value')
                            myrow['properties'].append(curation_property_value)
                            updateItem=updateItem or True
                        if not curation_detail_exists:
                            print ('Appending curation_detail_flag ' + self.createURLCurationProperty('', vproperty, vvalue)+"\n\n")
                            curation_detail_value={ "type": curationDetail, "value": self.createURLCurationProperty('', vproperty, vvalue)}
                            #print (f" ------ property before { myrow['properties']} \n")
                            myrow['properties'].append(curation_detail_value)
                            updateItem=updateItem or True
                    
                            #print (f"{updateItem}, {curation_detail_value}, pid: {toolpid}, \n { myrow['properties']}")
                    if vvalue.strip()=='200' or vvalue == 200 :
                        #print (f"check... \n")
                        myrow, flagsdropped=self._removeFlag(row[category], 'curation-flag-url', properties)
                        if (flagsdropped):
                            print (f'The item with PID: {category}/{toolpid} has a *no longer valid flag* for the property {vproperty}, will be removed. ({updateItem})')
                            updateItem=True
                            #print (f" dopo {myrow['properties']} \n")
                            
                            
                
                if updateItem and self.debug:
                    #print(self.getPutEP(category))
                    print ('\n*** Running in DEBUG mode, Marketplace dataset not updated. ***\n')
           
                          
                
                    
                    #save the updated description
                if (not self.debug) and updateItem and category.strip()!='':
                    print (f"updating item... ")
                    obj = json.dumps(myrow)
                    #put_url=self.dataset_entrypoints[itemscategory]+'/'
                    put_result=requests.put(self.getPutEP(category)+toolpid, data=obj, headers=put_headers)
                    print (put_result)
                
        if (not self.debug):
            print ('Reloading data from MP server, please wait...')
            self._getAllMPItems()
            print ('...done!')
            return res
    
    def removePropertyFlag(self, dataset, curationFlag, curationDetail):
        """
        
        Cheks if items in dataset have the curationFlag, removes the flag and  updates the MP data.
        
        Parameters:
        ----------
        
        dataset: DataFrame
            The set of items to be checked for flag
        curationFlag: String
            The Curation Flag property
        curationDetail: String
            The Curation Detail property
        
        When all items have been checked the function synchronizes the local dataset with the MP data, it may take several minutes to complete.
               
        """
        
        if (not isinstance(dataset, pd.DataFrame)):
            raise Exception(errno.ENOENT, os.strerror(errno.ENOENT), "dataset")
        
        
        res=pd.DataFrame()
        bearer=self.getBearer()
        
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        
        properties=dataset.property.unique().tolist();
        for key in self.dataset_entrypoints:
            if os.path.isfile('data/'+key+'.pickle') and not key=='actors':#local file is explicitly requested and one is available
                df_categ_all = pd.read_pickle('data/'+key+'.pickle')
            if df_categ_all.empty:
                print('INFO:'+key+' data not downloaded...')
                continue
       
            category=''
            for index, row in df_categ_all.iterrows():
                updateItem=False
                category=df_categ_all.columns[-1]
    
                toolpid=row[category]['persistentId']
                currentversion=row[category]['id']
                statustool=dataset[dataset['persistentId']==toolpid]
                if (not statustool.empty):
                    vproperty=str(dataset['property'])
                    myrow=row[category]
                    myrow, flagsdropped=self._removeFlag(row[category], curationFlag, properties)
                    if (flagsdropped):
                        print (f'The item with PID: {category}/{toolpid} has a *no longer valid flag* for the property {vproperty}, will be removed. ({updateItem})')
                        updateItem=True
            
        
        return
    
    
    #local functions
    def _removeFlag(self, row, curationFlag, cprops):
        #print (cprops)
        
        cflagidx=-1
        cdetidx=-1
        dropflag=False
        dropattrflag=False
        keyname=''
        if (curationFlag=='curation-flag-url'):
            keyname='url'
        else:
            #checkingFlag=json.loads(curationFlag)
            if (curationFlag['code']=="curation-flag-description"):
                keyname='description'
            
       
        for idx, ind in enumerate(row['properties']):
            
            if (ind['type']['code']== curationFlag): 
                cflagidx=idx
                
            if (ind['type']['code']=='curation-detail'):
                cur_de_val=ind['value']
                detail_value=json.loads(cur_de_val.replace("'", '"'))
                print (f"dv {detail_value}")
                if keyname in detail_value:
                    
                    idxl=[]
                    for purlidx, purl in enumerate(detail_value[keyname]):
                        
                        #print (f"PURL {purl}")
                        if list(purl)[0] in cprops:
                            cdetidx=idx
                            if (len(detail_value['url'])==1):
                                dropflag=True
                               
                            else:
                                dropattrflag=True
                                idxl.append(purlidx)
                        # else:
                        #     print (f"nah {list(purl)[0]}")
        if (dropattrflag & (cdetidx>-1)):
            idxl.sort(reverse=True)
            de_val=row['properties'][cdetidx]['value']
            flag_value=json.loads(de_val.replace("'", '"'))
            for ix in idxl:
                print(f"Dropping flag {flag_value['url'][ix]} ...")
                flag_value['url'].pop(ix)
                print ('done.')
            if(len(flag_value)==0):
                dropflag=True
            else:
                row['properties'][cdetidx]['value']=json.dumps(flag_value)
                dropflag=False
            
            
            #print(f"here {row['properties'][cdetidx]}")
            
        if (dropflag):
            print (f"Dropping all {curationFlag} flags:\n {row['properties'][cdetidx]['value']}...")
            if (cdetidx > cflagidx):
                row['properties'].pop(cdetidx)
                row['properties'].pop(cflagidx)
            else:
                row['properties'].pop(cflagidx)
                row['properties'].pop(cdetidx)
            print ('done.')
            #print (row['properties'])
       
            
             
        return row, (dropattrflag | dropflag)
    
    def _getDate(self):
        now = datetime.now()
        today_date=now.now()
        redate=today_date.strftime("%d/%m/%Y %H:%M:%S")
        return redate
    
    
    def _getLog(self):
        if os.path.isfile(self.datadir+'log.pickle'):
            df_log = pd.read_pickle(self.datadir+'log.pickle')
            return df_log
        else:
            print ('Creating log file...')
            df_log=pd.DataFrame()
            return df_log
    def _updateLog(self, df_log):
        if os.path.isfile(self.datadir+'log.pickle'):
            os.remove(self.datadir+'log.pickle')
        df_log.to_pickle(self.datadir+'log.pickle')
        
    def _addLogentry(self, df_log, log_entry):
        
        
        if not df_log.empty and log_entry['persistentId'] in df_log.persistentId.values and log_entry['restore_version'] in df_log.restore_version.values and log_entry['operation'] in df_log.operation.values:
            return df_log
        else:
            df_log = pd.concat([df_log, pd.DataFrame([log_entry])], ignore_index=True)
            return df_log
    
        
    #/local functions
    
    def setPropertyFlags(self, dataset, curationFlag, curationDetail):
        
        """
        Sets the curation flag for items in dataset and updates the MP data.
        
        Parameters:
        -----------
        
        dataset: DataFrame
            The set of items to be flagged
        curationFlag: String
            The Curation Flag property
        curationDetail: String
            The Curation Detail property
        
        When all items have been flagged the function synchronizes the local dataset with the MP data, it may take several minutes to complete.
               
        """
        res=pd.DataFrame()
        restoreset=self._getLog()
        bearer=self.getBearer()
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        
        
        for key in self.dataset_entrypoints:
            if os.path.isfile('data/'+key+'.pickle') and not key=='actors':#local file is explicitly requested and one is available
                df_categ_all = pd.read_pickle('data/'+key+'.pickle')
            if df_categ_all.empty:
                print('INFO:'+key+' data not downloaded...')
                continue
       
            category=''
            for index, row in df_categ_all.iterrows():
                updateItem=False
                category=df_categ_all.columns[-1]
    
                toolpid=row[category]['persistentId']
                currentversion=row[category]['id']
                statustool=dataset[dataset['persistentId']==toolpid]
                stdf=statustool[['value', 'property']]
                #st=statustool['value']
               
                vvalue=''
                value=''
                wrongvals={} 
                for vindex, vrow in stdf.iterrows():
                    vvalue=str(vrow['value'])
                    vproperty=str(vrow['property'])
                    
                    wrongvals[vproperty]=vvalue
                    print (f'The property: {vproperty}, has value {vvalue}, in item with pid: {category}/{toolpid}, (current version: {currentversion})')
                    
        
                for key in wrongvals:
                    vproperty=key
                    vvalue=wrongvals[key]
                    curation_property_exists=False
                    curation_detail_exists=False
                    curation_property_value={ "type": curationFlag, "value": "TRUE"}
    
                    myrow=row[category]
    
                    for ind in myrow['properties']:
                        #print (ind)
                        updateItem=False
                        
                        if (ind['type']['code']==curationFlag['code']):
                            
                            curation_property_exists=True
    
                        if (ind['type']['code']=='curation-detail'):
                            cur_de_val=ind['value']
                            #print(f'+++++++++++++++++++++++++++ {cur_de_val}')
                            curation_detail_exists=True
                            if vproperty in cur_de_val.strip():
                                print (f"flag property exists, value:  {ind['value']} \n")
                                updateItem=updateItem or False
                            else:
                                ind['value']=self.updateURLCurationPropertyJson(cur_de_val, vproperty, vvalue)
                                updateItem=updateItem or True
                                
               
                    if not curation_property_exists:
                        print ('append curation_property_value')
                        myrow['properties'].append(curation_property_value)
                        updateItem=updateItem or True
    
                    if not curation_detail_exists:
                        
                        curation_detail_value={ "type": curationDetail, "value": self.updateURLCurationPropertyJson('', vproperty, vvalue)}
                        print (f'append curation_detail_value {curation_detail_value}')
                        myrow['properties'].append(curation_detail_value)
                        updateItem=updateItem or True
                            #print (f"{updateItem}, {curation_detail_value}, pid: {toolpid}, \n { myrow['properties']}")
                if updateItem and self.debug:
                    #print(self.getPutEP(category))
                    print ('\n *** Running in DEBUG mode, Marketplace dataset not updated. *** \n')
                   
                    
                if (not self.debug) and updateItem and category.strip()!='':
                    print (f"updating item... ")
                    obj = json.dumps(myrow)
            
                    put_result=requests.put(self.getPutEP(category)+toolpid, data=obj, headers=put_headers)
                    print (put_result)
                    entryline={'date': _getdate(), 'persistentId': toolpid, 'category':category,'restore_version':currentversion, 'operation':curationFlag['code']}
                    restoreset=self._addLogentry(restoreset, entryline)
                
        if not restoreset.empty:
            self._updateLog(restoreset)
            
        if not self.debug:
            print ('Reloading data from MP server, please wait...')
            self._getAllMPItems()
            print ('done!')        
            return res
    
    
    # {'type': {'code': 'discipline'},
    #  'concept': {'code': '6020',
    #              'vocabulary': {'code': 'discipline'},
    #              'uri': 'https://vocabs.acdh.oeaw.ac.at/oefosdisciplines/6020'}
    #  }
    
    
    def updateItems(self, dataset, updateList, filterList):
        
        """
        Updates items in dataset by changing the content of the attributes contained in the updateList, the updated items are 
        stored in in the MP.
        
        Parameters:
        -----------
        
        dataset: DataFrame
            The set of items to be updated
        updateList: List
            The list of properties/attributes and values 
        filterList: List
            The list of filters
        When all items in the dataset have been updated the function synchronizes the local dataset with the MP data, 
        it may take several minutes to complete.
               
        """
        res=pd.DataFrame()
        restoreset=self._getLog()
        bearer=self.getBearer()
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        
        
        for key in self.dataset_entrypoints:
            if os.path.isfile('data/'+key+'.pickle') and not key=='actors':#local file is explicitly requested and one is available
                df_categ_all = pd.read_pickle('data/'+key+'.pickle')
            if df_categ_all.empty:
                print('INFO:'+key+' local data not present, please download MP dataset...')
                continue
       
            category=''
            for index, row in df_categ_all.iterrows():
                updateItem=False
                category=df_categ_all.columns[-1]
    
                toolpid=row[category]['persistentId']
                currentversion=row[category]['id']
                statustool=dataset[dataset['persistentId']==toolpid]
                oldkval=''
              
                for stindex, strow in statustool.iterrows():
                    for key in updateList:
                        oldkval=strow[key+".code"]
                       
                        myrow=row[category]
                        
                        for ind in myrow['properties']:
                            # print (ind)
                            if (('concept' in ind)  and ind[key]["code"]==oldkval and ind['concept']['code']==filterList["concept"]):

                                print (f'Changing the property:  "{key}", from  "{key}": {ind["type"]} \nto\n "{key}": {updateList[key]}", in item with pid: "{category}/{toolpid}"\n(Log info: current version is: {currentversion})\n')
                                
                                print (f'Changing the property:  "concept", from  "concept": {ind["concept"]} \nto\n "concept": {updateList["concept"]}", in item with pid: "{category}/{toolpid}"\n(Log info: current version is: {currentversion})\n')

                                ind[key]=updateList[key]
                                ind["concept"]=updateList["concept"]
                                updateItem=True
                                # print("Ecco:")
                                # print (ind)
                    if updateItem and self.debug:
                        print (json.dumps(myrow))
                        print ('\n *** Running in DEBUG mode, Marketplace dataset not updated. *** \n')
                   
                    
                    if (not self.debug) and updateItem and category.strip()!='':
                        print (f"updating item... ")
                        obj = json.dumps(myrow)
            
                        put_result=requests.put(self.getPutEP(category)+toolpid, data=obj, headers=put_headers)
                        print (put_result)
                        entryline={"date": _getdate(), "persistentId": toolpid, "category":category,"restore_version":currentversion, "operation":"update"}
                        restoreset=self._addLogentry(restoreset, entryline)
                
        if not restoreset.empty:
            self._updateLog(restoreset)
            
        if not self.debug:
            print ('Reloading data from MP server, please wait...')
            self._getAllMPItems()
            print ('done!')        
            return res
    
    
    def getMergedItem(self, category, pids):
        res=pd.DataFrame()
        if pids.strip()!='':
            persistentids=pids.replace(" ", "").split(',')
        else:
            print('No PID provided')
            return res
        if len(persistentids)<2:
            print('At least 2 PIDs are required')
            return res
        bearer=self.getBearer()
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        #{persistentId-item1}/merge?with={persistentId-item2}
        mitems=persistentids[1]
        mis = pd.Series(range(2, len(persistentids)))
        
        for mi in mis:
            mitems+=','+persistentids[mi]
            
        geturl=self.dataset_entrypoints[category]+'/'+persistentids[0]+'/merge?with='+mitems
        #print (geturl)
        merge_result=requests.get(geturl, headers=put_headers)
        jsono=merge_result.text
        #print (jsono)
        if (jsono.strip()==''):
            print ('Errors getting merged item')
            return res, ''
        jo=json.loads(jsono)
        #print(jo)
        res= pd.json_normalize(jo)
        items= pd.json_normalize(data=jo, record_path='properties', meta_prefix='ts_', meta=['label', 'persistentId', 'category'])
        #res=pd.merge(left=res, right=items, left_on='persistentId', right_on='ts_persistentId')
   
        npid=0
        for upid in persistentids:
            res['original_item_'+str(npid)]='<a target="_blank" href="'+self.MPserver+res['category']+'/'+upid+'">'+upid+'</a>'
            npid+=1
        return res.T, jo
        
      
    def postMergedItem(self, item, pids):
        if pids.strip()!='':
            persistentids=pids.replace(" ", "").split(',')
        else:
            print('No PID provided')
            return ''
        if len(persistentids)<2:
            print('At least 2 PIDs are required')
            return ''
        bearer=self.getBearer()
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        curation_merge_property={"code": "curation-flag-merged"}
        curation__merge_value={ "type": curation_merge_property, "value": "TRUE"}
        updateItem=True
        for ind in item['properties']:
            #print (ind)
            if (ind['type']['code']=='curation-flag-merged'):
                print(ind['type']['code'])
                updateItem=False
                print('Error, please check the items... maybe already merged?')
                return ''
        item['properties'].append(curation__merge_value)
        posturl=self.getPutEP(item['category'])+'merge?with='+pids.replace(' ','')
        obj = json.dumps(item)
        print ('Storing merged item for '+str(pids)+'...')
        print ('URL: '+posturl)
        if not self.debug and updateItem:
            merge_result_store=requests.post(posturl, data=obj, headers=put_headers)
            print ('Result Status Code: ')
            print (merge_result_store)
            mitem=merge_result_store.text
            print('Merged Item:')
            print (mitem)
            print ('...done.')
            return mitem
        if self.debug:
            print('...not executed, running in DEBUG mode.')
            
        
        return ''
    
    #(POST /api/actors/{id}/merge?with={id1},{id2}) 
    #{"code":"curation-flag-merged","label":"Curate merged items","type":"boolean","groupName":"Curation","hidden":true,"ord":39,"allowedVocabularies":[]}
    def postMergedActors(self, actorid, mergingids):
        if actorid.strip()=='':
            print('No ID provided for Actor')
            return ''
        
        if mergingids.strip()!='':
            persistentids=mergingids.replace(" ", "").split(',')
        else:
            print('No IDs provided for Actors to be merged')
            return ''
        
        bearer=self.getBearer()
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        curation_merge_property={"code": "curation-flag-merged"}
        curation__merge_value={ "type": curation_merge_property, "value": "TRUE"}
        updateItem=True
        
        
        posturl=self.getPutEP('actors')+actorid+'/merge?with='+mergingids.replace(' ','')
        
        print ('Merging actor '+actorid+' with actor(s) '+str(mergingids)+'...')
        print ('URL: '+posturl)
        if not self.debug and updateItem:
            merge_result_store=requests.post(posturl, headers=put_headers)
            print (f'Result Status Code: {merge_result_store}')
            mitem=merge_result_store.text
            print('Merged Item:')
            print (mitem)
            print ('...done.')
            return mitem
        if self.debug:
            print('...not executed, running in DEBUG mode.')
            
        
        return ''
    
    def getItemsforActor(self, actorid):
        if actorid.strip()=='':
            print('No id provided for Actor')
            return pd.DataFrame()
        turl=self.getPutEP('actors')+actorid+'?items=true'
        #turl='https://sshoc-marketplace-api-stage.acdh-dev.oeaw.ac.at/api/actors/2266?items=true' 
        print (f'Getting items for Actor id: {actorid} ...')
        df_desc_par=requests.get(turl)
        my=json.loads(df_desc_par.text)
        mydf=pd.DataFrame(my['items'])
        print ('...done.')
        return mydf
        
    def _getAllMPItems(self):
        self.getMPItems ("toolsandservices", False)
        self.getMPItems ("publications", False)
        self.getMPItems ("trainingmaterials", False)
        self.getMPItems ("workflows", False)
        self.getMPItems ("datasets", False)
    
    def getCategoryFilterValue(self, itemscategory):
        return self.categoryFilter.get(itemscategory)
    
    def getPutEP(self, itemscategory):
        if(itemscategory=='tools' or itemscategory=='tool-or-service'):
            return self.dataset_entrypoints['toolsandservices']+'/'
        if(itemscategory=='publications'):
            return self.dataset_entrypoints['publications']+'/'
        if(itemscategory=='trainingMaterials'):
            return self.dataset_entrypoints['trainingmaterials']+'/'
        if(itemscategory=='workflows'):
            return self.dataset_entrypoints['workflows']+'/'
        if(itemscategory=='datasets'):
            return self.dataset_entrypoints['datasets']+'/'
        if(itemscategory=='actors'):
            return self.dataset_entrypoints['actors']+'/'
    
    #RESTORE ITEMS
    
    
    def restoreItems(self, items):
       
        bearer=self.getBearer()
        put_headers = {'Content-type': 'application/json', 'Authorization':bearer}
        
        df_pids = pd.DataFrame()
        
        for index, row in items.iterrows():
            
            geturl=self.getPutEP(row['category'])
            restoreurl = geturl+str(row.persistentId)+'/versions/'+str(row.restore_version)+'/revert'
            print ('Restoring: '+restoreurl)
            if not self.debug:
                put_rest_result=requests.put(restoreurl, headers=put_headers)
                print (f"item {row.persistentId}, restored to the version {row.restore_version}")
                print (f"result {put_rest_result}")
            
            
            
            
    
