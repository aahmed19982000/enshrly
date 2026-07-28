def support_whatsapp(request):
    from syndicator.models import AISettings
    return {'support_whatsapp_number': AISettings.get_settings().support_whatsapp_number}
