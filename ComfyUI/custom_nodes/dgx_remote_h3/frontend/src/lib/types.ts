export type ProjectKind = 'drama' | 'mv';

export interface WorkbenchContext {
  project: any; episode: any; assets: any[]; shots: any[]; entities: any[]; capabilities: any;
  refresh(): Promise<void>;
  notify(message: string, tone?: 'error'|'success'|'info'): void;
  navigate(page: string, params?: Record<string,string>): void;
  openCanvas(shotId: string): void;
  params: Record<string,string>;
}
