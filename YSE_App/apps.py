from django.apps import AppConfig


class YseAppConfig(AppConfig):
    name = 'YSE_App'

    def ready(self):
        import YSE_App.signals  # noqa: F401
        from YSE_App.explorer_scope_hooks import install as install_explorer_scope

        install_explorer_scope()  # viewer-scoped SQL Explorer queries (#723)
