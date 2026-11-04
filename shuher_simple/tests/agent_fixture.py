import importlib
import os
from types import SimpleNamespace
from unittest.mock import patch
from concurrency import SheetsWorker
from sheet_fixtures import make_store
from agent_tools import AgentTools

class Harness:
    def __init__(self):
        env=dict(TELEGRAM_BOT_TOKEN='test',TELEGRAM_WEBHOOK_SECRET='test',OPENAI_API_KEY='test',SPREADSHEET_ID='test',OWNER_IDS='123')
        with patch.dict(os.environ,env),patch('store.SheetsStore'),patch('openai.AsyncOpenAI'):
            self.app=importlib.import_module('app')
        self.real_bot=self.app.bot_call
        self.store=make_store();self.worker=SheetsWorker();self.sent=[];self.failed=set()
        self.store.client.data['_Shuher_Users'].append(['789','worker2','Sergey','',''])
        self.store.client.data['_Shuher_Access'].append(['789','Доручення','read','123'])
        self.patches=[patch.object(self.app,'store',self.store),patch.object(self.app,'owners',{123}),
                      patch.object(self.app,'sheet_worker',self.worker),patch.object(self.app,'bot_call',self.bot),
                      patch.object(self.app,'history_store',None)]
        for p in self.patches:p.start()
    async def bot(self,method,payload=None,files=None):
        if payload.get('chat_id') in self.failed:raise RuntimeError('simulated delivery failure')
        self.sent.append((method,payload,files))
        return {'ok':True,'result':{'message_id':len(self.sent)}}
    def runtime(self,uid=123,event=1):
        return AgentTools(self.app,uid,uid,{'id':uid,'first_name':'Test'},self.store.get_dialogue(uid),event)
    def texts(self,uid):return '\n'.join(p['text'] for m,p,_ in self.sent if m=='sendMessage' and p['chat_id']==uid)
    def close(self):
        for p in reversed(self.patches):p.stop()
        self.worker.close()

class Responses:
    def __init__(self,*steps):self.steps=list(steps);self.calls=[]
    async def create(self,**kwargs):
        import copy
        self.calls.append(copy.deepcopy(kwargs))
        output,text=self.steps.pop(0)
        return SimpleNamespace(output=output,output_text=text,status='completed',usage=None)

def call(name,args,cid='c1'):
    import json
    return {'type':'function_call','call_id':cid,'name':name,'arguments':json.dumps(args)}
def client(*steps):return SimpleNamespace(responses=Responses(*steps))
