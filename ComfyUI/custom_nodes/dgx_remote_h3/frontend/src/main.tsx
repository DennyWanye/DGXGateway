import { createRoot } from 'react-dom/client';
import { Component, type ReactNode } from 'react';
import { App } from './App';
import './styles.css';
class ErrorBoundary extends Component<{children:ReactNode},{error:string}> {
 state={error:''};static getDerivedStateFromError(error:Error){return {error:error.message}}
 render(){return this.state.error?<div role="alert" style={{padding:40}}><h1>工作台暂时无法显示</h1><p>请保留当前页面，返回原工作台核查任务。重新打开不会自动提交生成。</p><pre>{this.state.error}</pre><button onClick={()=>location.reload()}>重新加载界面</button></div>:this.props.children}
}
createRoot(document.getElementById('root')!).render(<ErrorBoundary><App/></ErrorBoundary>);
