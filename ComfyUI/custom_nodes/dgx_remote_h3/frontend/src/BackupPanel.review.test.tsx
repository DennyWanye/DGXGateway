// @vitest-environment jsdom
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import {BackupPanel} from './BackupPanel';
import {get,post,uploadRestore} from '@/lib/api';
vi.mock('@/lib/api',()=>({get:vi.fn(),post:vi.fn(),uploadRestore:vi.fn(),requestKey:()=> 'new-operation-key',segment:encodeURIComponent,apiUrl:(p:string)=>'/dgx-drama'+p}));
const pid='a'.repeat(32), foreign='b'.repeat(32), rid='c'.repeat(32);
const ctx=()=>({project:{id:pid,title:'当前项目',revision:1},episode:{id:'e'.repeat(32)},assets:[],shots:[],entities:[],capabilities:{backupEnabled:true},refresh:vi.fn(),notify:vi.fn(),navigate:vi.fn(),openCanvas:vi.fn(),params:{}});
beforeEach(()=>{sessionStorage.clear();vi.clearAllMocks();history.replaceState({},'', '/');vi.mocked(get).mockResolvedValue({operations:[]});});
afterEach(cleanup);
it('retains a restore operation for an uploaded different-project package across refresh and remount',async()=>{
 const operation={id:rid,kind:'restore',projectId:foreign,state:'SUCCEEDED',result:{mode:'isolated'}};
 sessionStorage.setItem(`dgx.drama.backup.pending:${pid}:uploaded`,JSON.stringify({id:'d'.repeat(32),bytes:4,preview:{projectId:foreign,assetCount:1,tables:{dr_projects:1},snapshotKind:'stable'}}));
 vi.mocked(post).mockResolvedValue({operation});
 vi.mocked(get).mockImplementation(async path=> path===`/restores/${rid}`?{operation}:{operations:[]});
 let view=render(<BackupPanel ctx={ctx()}/>);
 fireEvent.click(screen.getByRole('button',{name:'恢复已检查项目包'}));
 fireEvent.click(screen.getByRole('button',{name:'确认继续'}));
 await waitFor(()=>expect(post).toHaveBeenCalledOnce());
 expect(await screen.findByRole('link',{name:'打开恢复工作区'})).toHaveAttribute('href',`/?restored=${rid}`);
 view.unmount(); view=render(<BackupPanel ctx={ctx()}/>);
 expect(await screen.findByRole('link',{name:'打开恢复工作区'})).toHaveAttribute('href',`/?restored=${rid}`);
 expect(post).toHaveBeenCalledOnce();
});
it('keeps the original unknown upload identity after same-name same-size different bytes conflict',async()=>{
 const key=`dgx.drama.backup.pending:${pid}`;
 sessionStorage.setItem(key,JSON.stringify({path:'/restore-uploads',body:{},key:'original-upload-key',upload:{name:'project.drama.zip',size:4}}));
 vi.mocked(uploadRestore).mockRejectedValue(Object.assign(new Error('原内容hash不匹配'),{status:409,code:'request_conflict'}));
 const context=ctx();render(<BackupPanel ctx={context}/>);
 fireEvent.change(screen.getByLabelText('项目备份文件'),{target:{files:[new File(['oops'],'project.drama.zip')]}});
 fireEvent.click(screen.getByRole('button',{name:'按原请求键核查归档操作'}));
 await waitFor(()=>expect(context.notify).toHaveBeenCalledWith('原内容hash不匹配','error'));
 expect(sessionStorage.getItem(key)).toContain('original-upload-key');
 expect(screen.getByRole('button',{name:'按原请求键核查归档操作'})).toBeInTheDocument();
 expect(uploadRestore).toHaveBeenCalledOnce();
});
