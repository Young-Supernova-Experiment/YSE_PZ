from django import template
register = template.Library()

@register.filter(name='snidsort')
def snidsort(name):
	addnums = 7-len(name)

	return "".join([name[:4],"".join(['1']*addnums),name[4:]])

