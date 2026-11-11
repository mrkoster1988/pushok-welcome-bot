"""Помощник новичка — самостоятельный первый сценарий."""
HELP = '/rules — правила\n/setrules текст — задать правила (админ)\n/agree — принять правила\n/mine — моё согласие'

def handle(s, u, cmd, arg, admin):
    from core import require_admin, parts, positive, add, item, listing

    if cmd == '/setrules':
        require_admin(admin)
        if not arg: raise ValueError('Добавьте текст правил.')
        s['rules'] = arg
        s['version'] = s.get('version', 0) + 1
        return 'Правила обновлены.'
    if cmd == '/rules': return s.get('rules', 'Администратор ещё не добавил правила.')
    if cmd == '/agree':
        if 'rules' not in s: raise ValueError('Правила ещё не заданы.')
        s.setdefault('agreements', {})[u] = s['version']
        return 'Согласие записано.'
    if cmd == '/mine':
        accepted = s.get('agreements', {}).get(u)
        return 'Актуальные правила приняты.' if accepted and accepted == s.get('version') else 'Нужно принять текущие правила.'
