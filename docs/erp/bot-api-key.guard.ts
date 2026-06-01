import {
  CanActivate,
  ExecutionContext,
  Injectable,
  UnauthorizedException,
} from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { Request } from 'express';

/**
 * Valida el header `X-Bot-Api-Key` contra la variable de entorno `BOT_API_KEY`.
 * Pensado para autenticar al chatbot (servicio a servicio), sin JWT de usuario.
 */
@Injectable()
export class BotApiKeyGuard implements CanActivate {
  constructor(private readonly config: ConfigService) {}

  canActivate(context: ExecutionContext): boolean {
    const expected = this.config.get<string>('BOT_API_KEY');
    if (!expected) {
      // Sin clave configurada, denegamos por seguridad.
      throw new UnauthorizedException('BOT_API_KEY no configurada');
    }
    const request = context.switchToHttp().getRequest<Request>();
    const provided = request.header('x-bot-api-key');
    if (!provided || provided !== expected) {
      throw new UnauthorizedException('API key inválida');
    }
    return true;
  }
}
