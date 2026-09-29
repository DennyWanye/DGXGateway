// @vitest-environment jsdom
import { useState } from 'react';
import { afterEach, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { ImageSizePicker } from './ImageSizePicker';
import { IMAGE_RATIOS, fitImageSize, imageDimensions } from '@/lib/image-size';
import { imageParameters } from './ProductionPanel';
afterEach(cleanup);
it('all ratio presets are valid for every generation model and LLaDA editing',()=>{
 for(const model of ['z-image-turbo','llada-image-turbo','flux2-dev','flux2-dev-abliterated'])
  for(const p of IMAGE_RATIOS) expect(imageParameters('generate_image',{model,prompt:'portrait',...p,seed:42,count:4,parallelism:4})).toMatchObject({width:p.width,height:p.height,batch_count:4,parallelism:4});
});
it('adapts 1080p export dimensions to exact 9:16 and checks model range, not just divisibility',()=>{
 expect(fitImageSize(1080,1920,'llada-image-turbo')).toEqual({width:864,height:1536});
 expect(()=>imageDimensions(1088,1920,'llada-image-turbo')).toThrow('256–1536');
 expect(()=>imageDimensions(1080,1920,'llada-image-turbo')).toThrow('864 × 1536');
 expect(imageDimensions(1088,1920,'z-image-turbo')).toEqual({width:1088,height:1920});
});
it('repairs an old invalid draft with one click and allows switching ratios',()=>{
 function Harness(){const [s,set]=useState({width:1080,height:1920});return <ImageSizePicker {...s} model="llada-image-turbo" project={{width:1080,height:1920}} onChange={(w,h)=>set({width:Number(w),height:Number(h)})}/>}
 render(<Harness/>);
 expect(screen.getByRole('alert')).toHaveTextContent('32 的倍数');
 fireEvent.click(screen.getByRole('button',{name:'适配为 864 × 1536'}));
 expect(screen.queryByRole('alert')).not.toBeInTheDocument();
 expect(screen.getByRole('button',{name:'9:16 竖屏'})).toHaveAttribute('aria-pressed','true');
 fireEvent.click(screen.getByRole('button',{name:'3:4 人像'}));
 expect(screen.getByText(/生成尺寸：768 × 1024/)).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'使用项目画幅'}));
 expect(screen.getByText(/生成尺寸：864 × 1536/)).toBeInTheDocument();
});
