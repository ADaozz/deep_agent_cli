import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({ plugins:[vue()], server:{host:'127.0.0.1',proxy:{'/api/agent':{target:'http://127.0.0.1:8765',configure(proxy){proxy.on('proxyRes',(response,_request,outgoing)=>{response.on('aborted',()=>outgoing.destroy());response.on('error',()=>outgoing.destroy());outgoing.on('close',()=>response.destroy())})}}}} })
