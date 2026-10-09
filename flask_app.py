import bridge_common as _common
import telegram_handlers as _telegram_handlers
import vk_handlers as _vk_handlers
import webhooks as _webhooks

app = _webhooks.app


def __getattr__(name):
    for module in (_webhooks, _telegram_handlers, _vk_handlers, _common):
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
