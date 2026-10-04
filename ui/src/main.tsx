import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './styles/base.css'
import './styles/desk.css'
import './styles/research.css'

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
