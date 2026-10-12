import {mount} from '@vue/test-utils'
import {it,expect} from 'vitest'
import TextWindow from '../components/TextWindow.vue'
it('keeps large source data intact while letting the user page earlier bounded DOM windows',async()=>{
 const text='first\n'+'x'.repeat(100000)+'\nlast'
 const w=mount(TextWindow,{props:{text}})
 expect(w.get('pre').text().length).toBeLessThanOrEqual(20000)
 expect(w.get('pre').text()).toContain('last')
 for(let i=0;i<6;i++)await w.get('button').trigger('click')
 expect(w.get('pre').text()).toContain('first')
 expect(w.props('text')).toBe(text)
})
it('does not split a Unicode surrogate pair at the visible window boundary',()=>{
 const w=mount(TextWindow,{props:{text:'prefix😀'+'x'.repeat(19999)}})
 expect(w.get('pre').text()).not.toContain('\uFFFD')
 expect(w.get('pre').text()).toContain('😀')
})
